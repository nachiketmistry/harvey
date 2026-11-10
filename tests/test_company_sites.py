"""COMPANY_SITE: a company's offices, from the schema.org address data on
its own pages."""

import json

import pytest

from sellbird.collectors.profile import ProfileCollector
from sellbird.locations import Place, parse_places, sites_from_schema

PAD = "x" * 600


def _page(*blocks):
    return "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks)


def addr(city="", region="", country=""):
    return {"@type": "PostalAddress", "addressLocality": city, "addressRegion": region,
            "addressCountry": country}


def test_organization_and_branch_addresses():
    html = _page({"@type": "Organization", "address": addr("Chicago", "IL", "US"),
                  "department": [{"@type": "LocalBusiness", "address": addr("Bellevue", "WA")}]},
                 {"@graph": [{"@type": "Place", "address": addr("Pittsburgh", "PA", "US")}]})
    # Each field from its own address field: addressRegion "WA" with no
    # country can't say which country (Washington or Western Australia).
    assert sites_from_schema(html) == [Place("Chicago", "US-IL", "US"), Place("Bellevue", "", ""),
                                       Place("Pittsburgh", "US-PA", "US")]


def test_other_peoples_places_are_skipped():
    html = _page({"@type": "Organization", "address": addr("Seattle", "WA", "US"),
                  "founder": {"@type": "Person", "address": addr("Miami", "FL")},
                  "event": {"@type": "Event", "location": {"@type": "Place", "address": addr("Las Vegas", "NV")}}},
                 {"@type": "JobPosting", "jobLocation": {"@type": "Place", "address": addr("Austin", "TX")}})
    assert sites_from_schema(html) == [Place("Seattle", "US-WA", "US")]


def test_unplaceable_and_duplicate_addresses():
    html = _page({"@type": "Organization", "address": "100 Main St"},
                 {"@type": "Organization", "address": [addr(country="US"), addr("Seattle", "WA"),
                                                       addr("seattle", "wa")]},
                 "not an object")
    assert sites_from_schema(html + "<script type='application/ld+json'>{broken</script>") == [
        Place(country="US"), Place("Seattle", "", "")]


def test_a_country_field_code_is_the_country():
    html = _page({"@type": "Organization", "address": addr("Berlin", "", "DE")},
                 {"@type": "Organization", "address": addr("Dover", "DE", "US")})
    assert sites_from_schema(html) == [Place("Berlin", "", "DE"), Place("Dover", "US-DE", "US")]


def test_places_round_trip_and_old_text_values():
    from sellbird.locations import places_value
    sites = [Place("Chicago", "US-IL", "US"), Place(country="DE")]
    assert parse_places(places_value(sites)) == sites
    assert parse_places("Chicago, IL, US | Bellevue, WA") == ["Chicago, IL, US", "Bellevue, WA"]
    assert parse_places("") == []


@pytest.mark.asyncio
async def test_profiler_records_homepage_and_locations_page_sites():
    pages = {
        "https://firm.example": f"<html>{PAD}" + _page(
            {"@type": "Organization", "address": addr("Chicago", "IL", "US")}) + "</html>",
        "https://firm.example/locations": f"<html>{PAD}" + _page(
            {"@type": "Organization", "location": [
                {"@type": "Place", "address": addr("Chicago", "IL", "US")},
                {"@type": "Place", "address": addr("Bellevue", "WA", "US")}]}) + "</html>",
    }
    c = ProfileCollector(None, {"COMPANY_SITE"})

    async def fake_get(url):
        return pages.get(url, "")
    c._get = fake_get
    [o] = await c.profile_company("c1", "firm.example")
    assert (o["signal_code"], o["value_num"]) == ("COMPANY_SITE", 2)
    assert parse_places(o["value_text"]) == [Place("Chicago", "US-IL", "US"), Place("Bellevue", "US-WA", "US")]
    assert o["evidence_url"] == "https://firm.example/locations"


@pytest.mark.asyncio
async def test_profiler_records_no_sites_too():
    c = ProfileCollector(None, {"COMPANY_SITE"})

    async def fake_get(url):
        return f"<html>{PAD}</html>" if url == "https://firm.example" else ""
    c._get = fake_get
    [o] = await c.profile_company("c1", "firm.example")
    assert (o["value_num"], o["value_text"]) == (0, "")
