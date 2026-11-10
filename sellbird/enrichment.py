"""Where people and companies are, from data providers.

Enrichment providers (Hunter, People Data Labs) keep cleaned, structured
records of people and companies, sourced under their own terms. A lookup
here asks one of them where a person works or where a company is, and
stores the answer as a PERSON_LOCATION or COMPANY_SITE observation
(``collector`` = the provider), field by field into a ``Place``. It then
takes part in the same precedence as everything else: what you state wins,
then your CRM, then providers, then what Sellbird read on the company's
own site and its free-text location.

Every provider is a small class behind one interface, with a monthly
allowance Sellbird counts itself (a lookup is counted before it's made, so
one that fails mid-flight still counts). Lookups are opt-in
(``enrichment.enabled``) and go only to the people the Writer is about to
draft, so a free tier's allowance lands on your best prospects first.

What each free tier returns (checked November 2026; plans change):

- Hunter: a person's or company's city, state and country (Clearbit
  format), one credit a lookup from the account's shared allowance.
- People Data Labs: 100 lookups a month, but the free plan hides a
  person's city and region and returns only country and continent; a
  company's location is returned in full. Paid plans return everything.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

from sellbird.locations import Place, place_from_fields, places_value

logger = logging.getLogger("sellbird.enrichment")

TIMEOUT = 15
# Trust against other sources (sellbird.geography takes the most confident
# person location): below you (1.0) and your CRM (0.9).
CONFIDENCE = {"pdl": 0.8, "hunter": 0.75}
# A person or company a provider has already been asked about, so it isn't
# asked (and charged) again: "enriched:<provider>:<kind>:<id>".
ASKED = "enriched:"


def _text(value) -> str:
    """A field's value when it's real text. Free plans return True/False in
    place of hidden fields, and those must never be read as a place."""
    return value.strip() if isinstance(value, str) else ""


class EnrichmentProvider:
    key = ""
    label = ""
    env_key = ""
    cap_env = ""          # env var holding the monthly allowance
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
        return f"enrichment_count:{self.key}:{datetime.now(timezone.utc):%Y-%m}"

    async def remaining(self, state) -> int:
        try:
            used = int(await state.get_setting(self._usage_key(), "0") or "0")
        except (TypeError, ValueError):
            used = 0
        return max(self.monthly_cap() - used, 0)

    async def spend(self, state) -> None:
        await state.increment_setting(self._usage_key())

    # Each provider counts a lookup against its allowance the way it's billed
    # (``spend``): before the request when every request costs, on a match
    # when only matches do.

    async def person(self, client, key: str, state, prospect, company) -> Place | None:
        return None

    async def company(self, client, key: str, state, company) -> Place | None:
        return None


class Hunter(EnrichmentProvider):
    """Hunter's Enrichment API in Clearbit format: ``geo`` with city, state
    code and country code. Draws on the same monthly allowance as Sellbird's
    other Hunter calls (HUNTER_MONTHLY_SEARCHES)."""
    key, label, env_key = "hunter", "Hunter", "HUNTER_API_KEY"

    def monthly_cap(self) -> int:
        from sellbird.integrations.email_finder import _hunter_monthly_cap
        return _hunter_monthly_cap()

    def _usage_key(self) -> str:
        from sellbird.integrations.email_finder import _hunter_usage_key
        return _hunter_usage_key()

    @staticmethod
    def _geo(data: dict) -> Place | None:
        geo = (data or {}).get("geo") or {}
        return place_from_fields(_text(geo.get("city")),
                                 _text(geo.get("stateCode")) or _text(geo.get("state")),
                                 _text(geo.get("countryCode")) or _text(geo.get("country")))

    async def _get(self, client, path: str, params: dict, key: str, state) -> dict | None:
        await self.spend(state)  # every request costs a credit
        r = await client.get(f"https://api.hunter.io/v2/{path}",
                             params={**params, "clearbit_format": "true"},
                             headers={"X-API-KEY": key})
        if r.status_code != 200:
            return None
        body = r.json() or {}
        return body.get("data") if isinstance(body.get("data"), dict) else body

    async def person(self, client, key, state, prospect, company) -> Place | None:
        if not getattr(prospect, "email", ""):
            return None
        data = await self._get(client, "people/find", {"email": prospect.email}, key, state)
        return self._geo(data) if data else None

    async def company(self, client, key, state, company) -> Place | None:
        if not getattr(company, "domain", ""):
            return None
        data = await self._get(client, "companies/find", {"domain": company.domain}, key, state)
        return self._geo(data) if data else None


class PeopleDataLabs(EnrichmentProvider):
    """People Data Labs' person and company enrichment. Only a match (HTTP
    200) is billed, and only a match is counted."""
    key, label, env_key = "pdl", "People Data Labs", "PDL_API_KEY"
    cap_env, default_cap = "PDL_MONTHLY_LOOKUPS", 100

    async def _get(self, client, path: str, params: dict, key: str, state) -> dict | None:
        r = await client.get(f"https://api.peopledatalabs.com/v5/{path}", params=params,
                             headers={"X-Api-Key": key})
        if r.status_code != 200:
            return None
        await self.spend(state)  # only a match is billed
        body = r.json() or {}
        return body.get("data") if isinstance(body.get("data"), dict) else body

    async def person(self, client, key, state, prospect, company) -> Place | None:
        if not getattr(prospect, "email", ""):
            return None
        data = await self._get(client, "person/enrich",
                               {"email": prospect.email, "min_likelihood": 6}, key, state)
        if not data:
            return None
        return place_from_fields(_text(data.get("location_locality")),
                                 _text(data.get("location_region")),
                                 _text(data.get("location_country")))

    async def company(self, client, key, state, company) -> Place | None:
        if not getattr(company, "domain", ""):
            return None
        data = await self._get(client, "company/enrich", {"website": company.domain}, key, state)
        loc = (data or {}).get("location") or {}
        if not isinstance(loc, dict):
            return None
        return place_from_fields(_text(loc.get("locality")), _text(loc.get("region")),
                                 _text(loc.get("country")))


PROVIDERS = {p.key: p for p in (Hunter(), PeopleDataLabs())}


def active_providers(config, env) -> list[EnrichmentProvider]:
    """Providers you turned on that have a key, in your order."""
    cfg = getattr(config, "enrichment", None)
    if not getattr(cfg, "enabled", False):
        return []
    return [PROVIDERS[k] for k in getattr(cfg, "providers", []) or []
            if k in PROVIDERS and PROVIDERS[k].configured(env)]


async def _asked(state, provider, kind: str, ident: str) -> bool:
    return bool(await state.get_setting(f"{ASKED}{provider.key}:{kind}:{ident}"))


async def enrich(state, config, env, prospects: list, limit: int,
                 client: httpx.AsyncClient | None = None) -> dict:
    """Ask each active provider where these people (and their companies)
    are, at most ``limit`` people, never asking a provider twice about the
    same person or company. Returns counts for the log."""
    from sellbird.geography import PERSON, prospect_place

    from sellbird.signals import seed_signal_catalog

    providers = active_providers(config, env)
    done = {"looked_up": 0, "placed": 0, "skipped_known": 0}
    if not providers or limit <= 0:
        return done
    # The heartbeat doesn't seed the vocabulary; an upgraded install may not
    # have PERSON_LOCATION yet, and the observations trigger would refuse it.
    await seed_signal_catalog(state)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        for prospect in prospects:
            if done["looked_up"] >= limit:
                break
            if (await prospect_place(state, prospect, config)).basis == PERSON:
                done["skipped_known"] += 1
                continue  # where they work is already known
            company = (await state.get_company(prospect.company_id)
                       if getattr(prospect, "company_id", "") else None)
            asked_any = person_found = site_found = placed = False
            for provider in providers:
                key = provider.api_key(env)
                if not person_found and not await _asked(state, provider, "person", prospect.id) \
                        and await provider.remaining(state) > 0:
                    asked_any = True
                    await state.set_setting(f"{ASKED}{provider.key}:person:{prospect.id}", "1")
                    found = await _safe(provider.person, client, key, state, prospect, company)
                    if found and found.known:
                        await state.add_observation(
                            "PERSON_LOCATION", prospect_id=prospect.id,
                            company_id=prospect.company_id or "", collector=provider.key,
                            value_text=found.to_json(), confidence=CONFIDENCE[provider.key])
                        placed = True
                        # A country alone (a free plan) leaves the city to
                        # the next provider; a city settles it.
                        person_found = bool(found.city or found.region)
                if company is not None and not site_found \
                        and not await _asked(state, provider, "company", company.id) \
                        and await provider.remaining(state) > 0:
                    asked_any = True
                    await state.set_setting(f"{ASKED}{provider.key}:company:{company.id}", "1")
                    site = await _safe(provider.company, client, key, state, company)
                    if site and site.known:
                        await state.add_observation(
                            "COMPANY_SITE", company_id=company.id, collector=provider.key,
                            value_text=places_value([site]), value_num=1,
                            confidence=CONFIDENCE[provider.key])
                        site_found = True
            if asked_any:
                done["looked_up"] += 1
            if placed:
                done["placed"] += 1
    finally:
        if owned:
            await client.aclose()
    return done


async def _safe(call, *args) -> Place | None:
    """A provider call that never raises: a provider that's down or returns
    something unexpected is a miss, not a stopped Writer."""
    try:
        return await call(*args)
    except Exception as e:
        logger.debug(f"enrichment call failed: {type(e).__name__}")
        return None
