"""COMPANY_SITE: a company's offices, from the schema.org address data on
its own pages."""

import json

import pytest

from sellbird.collectors.profile import ProfileCollector
from sellbird.locations import join_sites, sites_from_schema, split_sites

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
    assert sites_from_schema(html) == ["Chicago, IL, US", "Bellevue, WA", "Pittsburgh, PA, US"]


def test_other_peoples_places_are_skipped():
    html = _page({"@type": "Organization", "address": addr("Seattle", "WA", "US"),
                  "founder": {"@type": "Person", "address": addr("Miami", "FL")},
                  "event": {"@type": "Event", "location": {"@type": "Place", "address": addr("Las Vegas", "NV")}}},
                 {"@type": "JobPosting", "jobLocation": {"@type": "Place", "address": addr("Austin", "TX")}})
    assert sites_from_schema(html) == ["Seattle, WA, US"]


def test_unplaceable_and_duplicate_addresses():
    html = _page({"@type": "Organization", "address": "100 Main St"},
                 {"@type": "Organization", "address": [addr(country="US"), addr("Seattle", "WA"),
                                                       addr("seattle", "wa")]},
                 "not an object")
    assert sites_from_schema(html + "<script type='application/ld+json'>{broken</script>") == ["Seattle, WA"]


def test_sites_round_trip():
    sites = ["Chicago, IL, US", "Bellevue, WA"]
    assert split_sites(join_sites(sites)) == sites
    assert split_sites("") == []


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
    assert split_sites(o["value_text"]) == ["Chicago, IL, US", "Bellevue, WA, US"]
    assert o["evidence_url"] == "https://firm.example/locations"


@pytest.mark.asyncio
async def test_profiler_records_no_sites_too():
    c = ProfileCollector(None, {"COMPANY_SITE"})

    async def fake_get(url):
        return f"<html>{PAD}</html>" if url == "https://firm.example" else ""
    c._get = fake_get
    [o] = await c.profile_company("c1", "firm.example")
    assert (o["value_num"], o["value_text"]) == (0, "")
