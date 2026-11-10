"""Enrichment providers fill in where people and companies are.

Answers land as PERSON_LOCATION / COMPANY_SITE observations field by field,
below what you and your CRM state. Each provider is asked at most once per
person or company, within its monthly allowance, and a free plan's hidden
fields (True/False in place of the value) are never read as a place.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from sellbird.enrichment import PROVIDERS, active_providers, enrich
from sellbird.geography import PERSON, person_location, prospect_place
from sellbird.locations import Place, parse_places
from sellbird.models import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

PNW = ["Pacific Northwest", "United States", "Americas"]


def cfg(enabled=True, providers=("hunter", "pdl")):
    return SimpleNamespace(
        icp=SimpleNamespace(geography=PNW, geo_coordinates={}, regions={}, outside_geography="skip"),
        enrichment=SimpleNamespace(enabled=enabled, providers=list(providers), per_cycle=12))


ENV = SimpleNamespace(hunter_api_key="h-key", pdl_api_key="p-key")


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "sellbird.db"))
    asyncio.run(s.init_db())
    return s


async def _person(state, email="john@firm.example", location="Chicago, IL, USA"):
    cid = await state.add_company(Company(name="Firm", domain=email.split("@")[1], location=location))
    pid = await state.add_prospect(Prospect(first_name="John", last_name="Doe", company_id=cid,
                                            email=email, email_status="verified"))
    return await state.get_prospect(pid)


def transport(routes, calls):
    """Answers by URL path; records every request."""
    def handler(request):
        calls.append((request.url.host, request.url.path, dict(request.url.params),
                      request.headers.get("x-api-key")))
        status, body = routes.get(request.url.path, (404, {}))
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


HUNTER_PERSON = {"data": {"geo": {"city": "Pittsburgh", "state": "Pennsylvania", "stateCode": "PA",
                                  "country": "United States", "countryCode": "US"}}}
HUNTER_COMPANY = {"data": {"geo": {"city": "Chicago", "stateCode": "IL", "countryCode": "US"}}}
# PDL's free plan: city and region hidden behind booleans.
PDL_FREE_PERSON = {"status": 200, "data": {"location_locality": True, "location_region": True,
                                           "location_country": "united states"}}


def run(state, routes, prospects, config=None, limit=5):
    calls = []
    async def go():
        async with httpx.AsyncClient(transport=transport(routes, calls)) as client:
            return await enrich(state, config or cfg(), ENV, prospects, limit, client=client)
    return asyncio.run(go()), calls


def test_hunter_places_the_person_and_the_company(state):
    p = asyncio.run(_person(state))
    done, calls = run(state, {"/v2/people/find": (200, HUNTER_PERSON),
                              "/v2/companies/find": (200, HUNTER_COMPANY)}, [p],
                      cfg(providers=("hunter",)))
    assert done == {"looked_up": 1, "placed": 1, "skipped_known": 0}
    assert asyncio.run(person_location(state, p.id)) == Place("Pittsburgh", "US-PA", "US")
    placed = asyncio.run(prospect_place(state, p, cfg()))
    assert (placed.tier, placed.basis) == (1, PERSON)
    # The key goes in a header, never the URL; Clearbit format asked for.
    assert all(c[3] == "h-key" and c[2].get("clearbit_format") == "true" for c in calls)
    sites = asyncio.run(state.get_observations(company_id=p.company_id, signal_code="COMPANY_SITE"))
    assert parse_places(sites[0]["value_text"]) == [Place("Chicago", "US-IL", "US")]


def test_a_free_plans_hidden_fields_are_not_a_place(state):
    p = asyncio.run(_person(state))
    run(state, {"/v5/person/enrich": (200, PDL_FREE_PERSON)}, [p], cfg(providers=("pdl",)))
    # Only the country came back: enough to rule people out, not to place a city.
    assert asyncio.run(person_location(state, p.id)) == Place(country="US")


def test_each_provider_is_asked_once_per_person(state):
    p = asyncio.run(_person(state))
    routes = {"/v2/people/find": (404, {}), "/v5/person/enrich": (404, {})}
    run(state, routes, [p])
    _, calls = run(state, routes, [p])
    assert [c[1] for c in calls if "people" in c[1] or "person" in c[1]] == []


def test_counting_follows_each_providers_billing(state):
    p = asyncio.run(_person(state))
    hunter, pdl = PROVIDERS["hunter"], PROVIDERS["pdl"]
    before = (asyncio.run(hunter.remaining(state)), asyncio.run(pdl.remaining(state)))
    run(state, {}, [p])  # every lookup misses
    after = (asyncio.run(hunter.remaining(state)), asyncio.run(pdl.remaining(state)))
    # Hunter bills every request (person and company); PDL only a match.
    assert (before[0] - after[0], before[1] - after[1]) == (2, 0)


def test_no_allowance_left_means_no_request(state, monkeypatch):
    p = asyncio.run(_person(state))
    monkeypatch.setenv("PDL_MONTHLY_LOOKUPS", "0")
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "0")
    done, calls = run(state, {"/v2/people/find": (200, HUNTER_PERSON)}, [p])
    assert calls == [] and done["looked_up"] == 0


def test_people_already_placed_are_skipped_and_the_limit_holds(state):
    a = asyncio.run(_person(state, "a@one.example"))
    b = asyncio.run(_person(state, "b@two.example"))
    c = asyncio.run(_person(state, "c@three.example"))
    from sellbird.signals import seed_signal_catalog
    asyncio.run(seed_signal_catalog(state))
    asyncio.run(state.add_observation("PERSON_LOCATION", prospect_id=a.id, collector="you",
                                      value_text=Place("Seattle", "US-WA", "US").to_json()))
    done, calls = run(state, {}, [a, b, c], cfg(providers=("hunter",)), limit=1)
    assert done == {"looked_up": 1, "placed": 0, "skipped_known": 1}
    assert {c[2].get("email") for c in calls if c[1] == "/v2/people/find"} == {"b@two.example"}


def test_off_or_without_a_key_nothing_runs(state):
    assert active_providers(cfg(enabled=False), ENV) == []
    assert [p.key for p in active_providers(cfg(), SimpleNamespace(hunter_api_key="", pdl_api_key="k"))] == ["pdl"]


def test_a_provider_error_is_a_miss(state):
    p = asyncio.run(_person(state))

    def boom(request):
        raise httpx.ConnectError("down")
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
            return await enrich(state, cfg(providers=("hunter",)), ENV, [p], 5, client=client)
    assert asyncio.run(go())["placed"] == 0


def test_a_country_from_one_provider_is_refined_by_anothers_city(state):
    # PDL (more trusted, free plan) says the US; Hunter says Pittsburgh.
    p = asyncio.run(_person(state))
    done, _ = run(state, {"/v5/person/enrich": (200, PDL_FREE_PERSON),
                          "/v2/people/find": (200, HUNTER_PERSON)}, [p], cfg(providers=("pdl", "hunter")))
    assert done["placed"] == 1
    assert asyncio.run(person_location(state, p.id)) == Place("Pittsburgh", "US-PA", "US")


def test_a_disagreeing_less_trusted_source_does_not_refine(state):
    from sellbird.signals import seed_signal_catalog
    p = asyncio.run(_person(state))
    asyncio.run(seed_signal_catalog(state))
    asyncio.run(state.add_observation("PERSON_LOCATION", prospect_id=p.id, collector="pdl",
                                      value_text=Place(country="CA").to_json(), confidence=0.8))
    asyncio.run(state.add_observation("PERSON_LOCATION", prospect_id=p.id, collector="hunter",
                                      value_text=Place("Seattle", "US-WA", "US").to_json(), confidence=0.75))
    assert asyncio.run(person_location(state, p.id)) == Place(country="CA")
