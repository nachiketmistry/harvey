"""Who to write to at a company you already chose: people search.

This is the account-based step lead databases call "people at this account
by persona" (Apollo's people search, ZoomInfo's contacts at a company, Sales
Navigator's account filter): the company is decided (it fits a segment),
and the person who runs the thing you sell to isn't on file. Sellbird asks a
people-data provider for people at the company's domain whose title is one
the company's segment targets (``icp.segments[].titles``, else
``icp.titles``), keeps their name and title, and finds their address the way
it finds every address: the company's learned email pattern (taught by the
colleagues already on file), then one verified candidate
(``sellbird.integrations.email_finder``). The provider's own email isn't
used, so a free plan that hides contact data loses nothing.

Every provider is a small class behind one interface (``PeopleFinder``),
with a monthly allowance Sellbird counts itself, the way it's billed. A
company is searched once per provider unless you ask again, so the
allowance isn't spent twice on the same answer.

What each provider returns (checked November 2026; plans change):

- People Data Labs Person Search: name, job title and LinkedIn URL on the
  free plan (contact fields come back as true/false). Billed one search
  credit per record returned; a search with no match costs nothing. Its
  search allowance is separate from its enrichment lookups.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from sellbird.models.prospect import Prospect

logger = logging.getLogger("sellbird.people")

TIMEOUT = 30
SEARCHED = "people_searched:"   # "people_searched:<provider>:<company id>"
SOURCE = "{key}_person_search"
_DOMAIN = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass(frozen=True)
class FoundPerson:
    first_name: str
    last_name: str
    title: str
    linkedin_url: str = ""


@dataclass
class SearchOutcome:
    """What a run did, for the CLI to report."""
    searched: int = 0
    no_match: int = 0
    skipped_searched: int = 0
    skipped_no_titles: int = 0
    out_of_allowance: bool = False
    added: list = field(default_factory=list)       # (company name, Prospect)
    already_known: int = 0
    failed: int = 0


class PeopleFinder:
    key = ""
    label = ""
    env_key = ""
    cap_env = ""
    default_cap = 0

    def api_key(self, env) -> str:
        return (getattr(env, self.env_key.lower(), "") or "").strip()

    def configured(self, env) -> bool:
        return bool(self.api_key(env))

    def monthly_cap(self) -> int:
        raw = os.getenv(self.cap_env, "").strip() if self.cap_env else ""
        try:
            return max(int(raw), 0) if raw else self.default_cap
        except ValueError:
            return self.default_cap

    def _usage_key(self) -> str:
        return f"people_search_count:{self.key}:{datetime.now(timezone.utc):%Y-%m}"

    def _reported_key(self) -> str:
        return f"people_search_left:{self.key}:{datetime.now(timezone.utc):%Y-%m}"

    async def remaining(self, state) -> int:
        """Our own count against the cap, or what the provider last said is
        left this month when that's lower (credits spent elsewhere)."""
        try:
            used = int(await state.get_setting(self._usage_key(), "0") or "0")
        except (TypeError, ValueError):
            used = 0
        left = max(self.monthly_cap() - used, 0)
        try:
            reported = await state.get_setting(self._reported_key(), "")
            return min(left, max(int(reported), 0)) if reported != "" else left
        except (TypeError, ValueError):
            return left

    async def note_reported(self, state, value) -> None:
        try:
            await state.set_setting(self._reported_key(), str(max(int(value), 0)))
        except (TypeError, ValueError):
            pass

    async def spend(self, state, n: int = 1) -> None:
        for _ in range(max(n, 0)):
            await state.increment_setting(self._usage_key())

    async def search(self, client, key: str, state, domain: str, titles: list[str],
                     size: int) -> list[FoundPerson] | None:
        """People at ``domain`` whose title matches one of ``titles``, at most
        ``size``. [] when nobody matches, None when the search failed."""
        raise NotImplementedError


def _sql_text(value: str) -> str:
    """A title as a SQL LIKE fragment: lower case, quotes doubled, LIKE's own
    wildcards removed so a title can't widen the match."""
    return re.sub(r"[%_\\]", " ", (value or "").strip().lower()).replace("'", "''")


_ACRONYMS = {"it", "ii", "iii", "iv", "soc", "siem", "sre", "ciso", "cto", "cio", "cso",
             "vp", "svp", "evp", "qa", "aws", "gcp", "ai", "ml", "noc", "grc", "iam"}
_CASED = {"devops": "DevOps", "devsecops": "DevSecOps", "infosec": "InfoSec", "secops": "SecOps"}
_SMALL = {"and", "of", "the", "for", "in", "at", "to", "a", "an", "&"}


def title_case(text: str) -> str:
    """A job title as people write it: providers store "senior it and
    security engineer ii", shown as "Senior IT and Security Engineer II"."""
    out = []
    for i, word in enumerate((text or "").split()):
        bare = word.lower().strip(",|/()")
        if bare in _ACRONYMS:
            cased = bare.upper()
        elif bare in _CASED:
            cased = _CASED[bare]
        elif i and bare in _SMALL:
            cased = bare
        else:
            cased = bare[:1].upper() + bare[1:]
        out.append(word.lower().replace(bare, cased, 1) if bare else word)
    return " ".join(out)


def _name_part(value) -> str:
    text = str(value or "").strip()
    return text[:1].upper() + text[1:] if text else ""


class PDLPeopleSearch(PeopleFinder):
    """People Data Labs' Person Search, by company website and title. Billed
    per record returned, so a record is counted once it comes back."""
    key, label, env_key = "pdl", "People Data Labs", "PDL_API_KEY"
    cap_env, default_cap = "PDL_MONTHLY_SEARCHES", 100

    def query(self, domain: str, titles: list[str]) -> str:
        likes = " OR ".join(f"job_title LIKE '%{t}%'" for t in
                            dict.fromkeys(_sql_text(t) for t in titles) if t.strip())
        return f"SELECT * FROM person WHERE job_company_website = '{domain}' AND ({likes})"

    async def search(self, client, key, state, domain, titles, size):
        domain = (domain or "").strip().lower()
        if not _DOMAIN.match(domain) or not titles or size <= 0:
            return []
        try:
            r = await client.post("https://api.peopledatalabs.com/v5/person/search",
                                  json={"sql": self.query(domain, titles), "size": size},
                                  headers={"X-Api-Key": key})
        except httpx.HTTPError as e:
            logger.debug(f"PDL person search failed for {domain}: {type(e).__name__}")
            return None
        if r.headers.get("x-totallimit-remaining") is not None:
            await self.note_reported(state, r.headers["x-totallimit-remaining"])
        if r.status_code == 404:
            return []                      # no match, not billed
        if r.status_code != 200:
            logger.info(f"PDL person search for {domain}: HTTP {r.status_code}")
            return None
        rows = (r.json() or {}).get("data") or []
        await self.spend(state, len(rows))
        found = []
        for row in rows:
            first, last = _name_part(row.get("first_name")), _name_part(row.get("last_name"))
            if first and last:
                found.append(FoundPerson(first, last, title_case(str(row.get("job_title") or "")),
                                         str(row.get("linkedin_url") or "")))
        return found


FINDERS = {f.key: f for f in (PDLPeopleSearch(),)}


def active_finder(env, key: str = "") -> PeopleFinder | None:
    """The finder to use: the one named, else the first with a key."""
    if key:
        f = FINDERS.get(key)
        return f if f and f.configured(env) else None
    return next((f for f in FINDERS.values() if f.configured(env)), None)


def _norm_name(first: str, last: str) -> tuple[str, str]:
    return (first or "").strip().lower(), (last or "").strip().lower()


async def find_people(state, env, companies: list, titles_for, per_company: int = 2,
                      finder: PeopleFinder | None = None, again: bool = False,
                      find_email=None) -> SearchOutcome:
    """Search each company for people whose title its segment targets and
    add them as new prospects with a graded address.

    ``titles_for(company)`` gives the titles to search for. ``find_email`` is
    the address resolver (``email_finder.find_email`` by default), passed in
    for tests."""
    if find_email is None:
        from sellbird.integrations.email_finder import find_email
    out = SearchOutcome()
    finder = finder or active_finder(env)
    if finder is None:
        return out
    key = finder.api_key(env)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for company in companies:
            titles = titles_for(company) or []
            if not titles:
                out.skipped_no_titles += 1
                continue
            marker = f"{SEARCHED}{finder.key}:{company.id}"
            if not again and await state.get_setting(marker):
                out.skipped_searched += 1
                continue
            room = min(per_company, await finder.remaining(state))
            if room <= 0:
                out.out_of_allowance = True
                break
            found = await finder.search(client, key, state, company.domain, titles, room)
            if found is None:
                out.failed += 1
                continue
            await state.set_setting(marker, datetime.now(timezone.utc).isoformat())
            out.searched += 1
            if not found:
                out.no_match += 1
                continue
            on_file = await state.get_prospects_for_company(company.id)
            names = {_norm_name(p.first_name, p.last_name) for p in on_file}
            known = [(p.email, p.first_name, p.last_name) for p in on_file
                     if p.email and p.first_name and p.last_name
                     and p.email_status in ("verified", "risky")]
            others = [(p.first_name, p.last_name) for p in on_file
                      if p.first_name and p.last_name]
            for person in found:
                if _norm_name(person.first_name, person.last_name) in names:
                    out.already_known += 1
                    continue
                try:
                    result = await find_email(
                        person.first_name, person.last_name, company.domain,
                        verify=True, env=env, state=state,
                        known_emails=list(known) or None, other_people=list(others) or None)
                    email, status = result.email, result.status
                except Exception as e:
                    logger.debug(f"People: address lookup failed at {company.domain}: {e}")
                    email, status = "", "guess"
                prospect = Prospect(
                    company_id=company.id, first_name=person.first_name,
                    last_name=person.last_name, title=person.title,
                    linkedin_url=person.linkedin_url, email=email,
                    email_status=status, email_verified=status == "verified",
                    source=SOURCE.format(key=finder.key), status="new",
                    company=company.name or company.domain,
                )
                prospect.id = await state.add_prospect(prospect)
                names.add(_norm_name(person.first_name, person.last_name))
                others.append((person.first_name, person.last_name))
                out.added.append((company.name or company.domain, prospect))
    return out
