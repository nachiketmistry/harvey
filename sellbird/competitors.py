"""Competitors: who they are, what to say about them, and who names them.

A CRM keeps competitors as their own records (Salesforce's Competitor
object, HubSpot's competitor property) with a battlecard each (Klue,
Crayon), and a call recorder flags every time a buyer names one (Gong's
keyword trackers). Sellbird does the same for email:

- A competitor profile (``competitors``): a name, an optional domain and
  company, the aliases prospects use for it, and a battlecard you write.
  Marking a company as a competitor creates one; you can also add vendors
  Sellbird never found and products that aren't companies at all.
- Mention detection: every reply is matched, deterministically and before
  any model call, against each competitor's name, domain and aliases. A
  match is a COMPETITOR_MENTIONED observation on the person and their
  company (the competitor, the words that matched, a short excerpt), and
  that competitor's battlecard goes into the prompt for the answer.
- A company on your competitor list by domain is never cold-emailed, even
  one found after you added it (``StateManager.get_company_relationship``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

SIGNAL = "COMPETITOR_MENTIONED"
MIN_TERM = 3          # shorter aliases match too much ordinary text
EXCERPT = 90          # characters either side of a mention, for the record
BATTLECARD_MAX = 4000  # characters of one battlecard in a prompt


@dataclass(frozen=True)
class Mention:
    competitor_id: str
    name: str
    term: str
    excerpt: str

    def to_json(self) -> str:
        return json.dumps({"competitor_id": self.competitor_id, "name": self.name,
                           "term": self.term, "excerpt": self.excerpt})


def terms(competitor: dict) -> list[str]:
    """The words that name this competitor: its name, its domain and the
    domain's first label ("deslicer" for deslicer.com), and its aliases."""
    domain = (competitor.get("domain") or "").lower()
    found = [competitor.get("name") or "", domain, domain.split(".")[0] if domain else ""]
    found += competitor.get("aliases") or []
    clean = [" ".join(t.split()) for t in found if t]
    return list(dict.fromkeys(t for t in clean if len(t) >= MIN_TERM))


def _pattern(term: str) -> re.Pattern:
    # Whole words only, whitespace-flexible: "Splunk  AI" matches
    # "splunk ai", and "Drew" doesn't match "Andrew".
    body = r"\s+".join(re.escape(w) for w in term.split())
    return re.compile(rf"(?<![\w-]){body}(?![\w-])", re.IGNORECASE)


def _excerpt(text: str, start: int, end: int) -> str:
    a, b = max(start - EXCERPT, 0), min(end + EXCERPT, len(text))
    snippet = " ".join(text[a:b].split())
    return ("…" if a else "") + snippet + ("…" if b < len(text) else "")


def find_mentions(text: str, competitors: list[dict]) -> list[Mention]:
    """Each competitor named in ``text`` once, with the longest term that
    matched (so "Splunk AI Assistant" wins over "Splunk AI")."""
    if not text:
        return []
    found = []
    for comp in competitors:
        best = None
        for term in sorted(terms(comp), key=len, reverse=True):
            m = _pattern(term).search(text)
            if m:
                best = Mention(comp["id"], comp.get("name") or term, term,
                               _excerpt(text, m.start(), m.end()))
                break
        if best:
            found.append(best)
    return found


def battlecard_block(mentions: list[Mention], competitors: list[dict]) -> str:
    """The prompt section for a reply that names competitors: who they
    named, and your battlecard for each one that has one."""
    if not mentions:
        return ""
    cards = {c["id"]: (c.get("battlecard") or "").strip() for c in competitors}
    out = ["COMPETITORS THEY NAMED",
           "The prospect named a competitor. Answer what they actually said about it. "
           "Be fair to the competitor: never disparage it, never claim what the "
           "battlecard doesn't support, and don't bring up a competitor they didn't name."]
    for m in mentions:
        card = cards.get(m.competitor_id, "")
        # Only your own words here: what they wrote is in the conversation,
        # fenced as untrusted, and must not reach the prompt outside it.
        out.append(f"\n{m.name} (named as \"{m.term}\")")
        out.append(card[:BATTLECARD_MAX] if card else
                   "No battlecard for this competitor; rely on the product knowledge above.")
    return "\n".join(out)


async def mentions_in(state, text: str) -> tuple[list[Mention], list[dict]]:
    """The competitors named in ``text``, and the competitor list (for the
    battlecards). Never raises: a failed lookup is no mention."""
    try:
        competitors = await state.get_competitors()
    except Exception:
        return [], []
    return find_mentions(text, competitors), competitors


async def record_mentions(state, prospect, mentions: list[Mention], collector: str = "handler") -> None:
    if not mentions:
        return
    from sellbird.signals import seed_signal_catalog
    # The heartbeat doesn't seed the vocabulary, and the observations
    # trigger refuses a code it doesn't know (an upgraded install).
    await seed_signal_catalog(state)
    for m in mentions:
        await state.add_observation(
            SIGNAL, prospect_id=prospect.id, company_id=getattr(prospect, "company_id", "") or "",
            collector=collector, value_text=m.to_json(), confidence=1.0)


async def save_profile(state, name: str, domain: str = "", aliases: list[str] | None = None,
                       battlecard: str | None = None, competitor_id: str = "",
                       note: str = "", source: str = "") -> str:
    """Add or edit a competitor (by id when given), and when its domain is a
    company Sellbird found, mark that company as a competitor so it gets no
    cold email. Returns the competitor's id."""
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0].removeprefix("www.")
    if competitor_id:
        current = await state.find_competitor(competitor_id)
        if current is None:
            raise ValueError("No such competitor.")
        await state.update_competitor(competitor_id, name, domain,
                                      current["battlecard"] if battlecard is None else battlecard)
        cid = competitor_id
    else:
        cid = await state.save_competitor(name, domain, battlecard=battlecard)
    if aliases is not None:
        await state.set_competitor_aliases(cid, aliases)
    if domain:
        company = await state.get_company_by_domain(domain)
        if company is not None:
            rel = await state.get_company_relationship(company.id)
            if not rel or rel.get("relationship") != "competitor" or rel.get("note") == "On your competitor list":
                await state.set_company_relationship(company.id, "competitor", note.strip())
                if source:
                    await state.log_action("company_relationship", source, {
                        "company_id": company.id, "domain": domain,
                        "relationship": "competitor", "note": note.strip()})
    return cid


_CARD = re.compile(r"^##\s+vs\.?\s+(.+?)\s*$", re.MULTILINE)
_PAREN = re.compile(r"\(([^()]*)\)")
_EXAMPLES = re.compile(r"^(?:e\.g\.|i\.e\.|such as|like)[,:]?\s*", re.IGNORECASE)


def _heading(text: str) -> tuple[str, list[str]]:
    """A battle-card heading as (name, aliases). Training writes headings
    like "Monitoring Console / Cloud Console (built-in health checks)" or
    "General chatbots (e.g., Foo, Bar)": each side of a spaced slash and
    each parenthesised name or example is an alias, so it can match."""
    aliases = []
    for inner in _PAREN.findall(text):
        inner = inner.strip()
        if _EXAMPLES.match(inner):
            aliases += [a.strip() for a in _EXAMPLES.sub("", inner).split(",")]
        else:
            aliases.append(inner)
    name = " ".join(_PAREN.sub(" ", text).split())
    parts = [p.strip() for p in name.split(" / ")]
    if len(parts) > 1:
        aliases = parts + aliases
    return name, [a for a in dict.fromkeys(aliases) if a and a.lower() != name.lower()]


def battlecards_from_skill(markdown: str) -> list[tuple[str, list[str], str]]:
    """(name, aliases, battlecard) for each "## vs. NAME" section of a
    battle-cards skill file (what ``sellbird train`` writes to
    skills/competitive_intel.md)."""
    heads = list(_CARD.finditer(markdown or ""))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(markdown)
        body = markdown[h.end():end].strip()
        body = re.sub(r"\n-{3,}\s*$", "", body).strip()
        name, aliases = _heading(h.group(1))
        if name:
            out.append((name, aliases, body))
    return out
