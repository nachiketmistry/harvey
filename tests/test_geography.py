"""icp.geography is the territory in priority order: earlier entries are
written to first, and a company placed outside every entry is not contacted."""

import asyncio
from types import SimpleNamespace

from sellbird.agents.writer import Writer
from sellbird.fit import company_fit
from sellbird.geography import (
    COMPANY, PERSON, PRESENCE, UNKNOWN, matches, outside, place, prospect_place, rank, tier,
)


def config(geography=("Denver, CO", "United States", "Canada"), coords=None,
           regions=None, outside_geography="skip"):
    return SimpleNamespace(icp=SimpleNamespace(
        geography=list(geography),
        geo_coordinates=coords if coords is not None else {"Denver, CO": "39.7392,-104.9903,50"},
        regions=regions or {}, outside_geography=outside_geography,
        industries=[], titles=[], hiring_signals=[],
    ), compliance=SimpleNamespace(do_not_contact=[]))


def test_city_entry_matches_by_name():
    assert tier("Denver, CO", config()) == (0, "Denver, CO")
    assert tier("Denver, Colorado, USA", config()) == (0, "Denver, CO")


def test_city_entry_matches_inside_its_radius():
    # A suburb ~15 km out, known only by its cached coordinates.
    assert tier("Lakewood, CO", config(), point="39.7047,-105.0814") == (0, "Denver, CO")
    # Same state, outside the 50 km circle: falls to the country tier.
    assert tier("Pueblo, CO", config(), point="38.2544,-104.6091") == (1, "United States")


def test_country_entries_match_by_country():
    assert tier("Austin, TX", config()) == (1, "United States")
    assert tier("US", config()) == (1, "United States")
    assert tier("Toronto, ON", config()) == (2, "Canada")
    assert tier("Canada", config()) == (2, "Canada")


def test_same_city_name_in_another_country_does_not_match():
    assert not matches("London, UK", "London, ON, Canada")
    assert matches("London, UK", "London, United Kingdom")


def test_outside_every_entry_ranks_last_but_is_kept():
    cfg = config()
    assert tier("Lyon, France", cfg) == (None, "")
    assert rank("Lyon, France", cfg) == 3
    assert rank("", cfg) == 3
    assert rank("Denver, CO", cfg) < rank("Austin, TX", cfg) < rank("Toronto, ON", cfg) < rank("FR", cfg)


def test_no_geography_ranks_everyone_equal():
    cfg = config(geography=(), coords={})
    assert rank("Denver, CO", cfg) == rank("Lyon, France", cfg) == 0


def test_region_names_match_only_by_name():
    cfg = config(geography=("Western Europe",), coords={})
    assert tier("Western Europe", cfg) == (0, "Western Europe")
    assert tier("Lyon, France", cfg) == (None, "")


def test_fit_reports_location_without_changing_the_score():
    company = {"id": "c1", "domain": "acme.example", "industry": "", "location": "Toronto, ON"}
    score, reasons = company_fit(company, [], [], config())
    assert score == 0
    assert "location: Canada (priority 3): company is in Toronto, ON" in reasons
    far = {**company, "location": "Lyon, France"}
    assert company_fit(far, [], [], config())[1][-1] == "location: outside your geography (not contacted)"
    kept = config(outside_geography="last")
    assert company_fit(far, [], [], kept)[1][-1] == "location: outside your geography (contacted last)"
    assert not any(r.startswith("location") for r in company_fit({**company, "location": ""}, [], [], config())[1])


class FakeState:
    """Companies by id with one location each; optional offices per company
    (COMPANY_SITE) and locations per person (PERSON_LOCATION)."""
    def __init__(self, places, sites=None, people=None):
        self.places, self.sites, self.people = places, sites or {}, people or {}

    async def get_company(self, company_id):
        return SimpleNamespace(location=self.places.get(company_id, ""))

    async def get_setting(self, key):
        return None

    async def get_observations(self, company_id="", signal_code="", latest_only=False, limit=500):
        sites = self.sites.get(company_id)
        return [{"value_text": " | ".join(sites)}] if sites and signal_code == "COMPANY_SITE" else []

    async def get_prospect_observations(self, prospect_id, signal_code):
        return [{"value_text": v, "confidence": c, "observed_at": t}
                for v, c, t in self.people.get(prospect_id, [])] if signal_code == "PERSON_LOCATION" else []


def test_writer_orders_by_tier_then_score():
    writer = Writer.__new__(Writer)
    writer.config = config()
    writer.state = FakeState({"far": "Lyon, France", "us": "Austin, TX", "home": "Denver, CO", "ca": "Toronto, ON"})
    p = lambda pid, cid, score: SimpleNamespace(id=pid, company_id=cid, score=score)
    prospects = [p("1", "far", 99), p("2", "us", 40), p("3", "home", 10), p("4", "us", 80),
                 p("5", "ca", 90), p("6", "", 95)]
    ordered = asyncio.run(writer._prioritise(prospects))
    assert [x.id for x in ordered] == ["3", "4", "2", "5", "1", "6"]


PNW = ("Pacific Northwest", "United States", "Americas")


def test_built_in_regions_match_by_state_and_country():
    cfg = config(geography=PNW, coords={})
    assert tier("Washougal, WA", cfg) == (0, "Pacific Northwest")
    assert tier("Portland, Oregon", cfg) == (0, "Pacific Northwest")
    assert tier("Boise, ID", cfg) == (0, "Pacific Northwest")
    assert tier("Vancouver, BC", cfg) == (0, "Pacific Northwest")
    assert tier("Oakland, CA", cfg) == (1, "United States")
    assert tier("Toronto, ON", cfg) == (2, "Americas")
    assert tier("Sao Paulo, Brazil", cfg) == (2, "Americas")
    assert tier("MX", cfg) == (2, "Americas")
    assert tier("HK", cfg) == (None, "")
    # Perth, Western Australia is not Washington state.
    assert tier("Perth, WA, Australia", cfg) == (None, "")


def test_configured_regions_add_to_and_override_built_ins():
    cfg = config(geography=("DACH", "PNW"), coords={},
                 regions={"DACH": ["DE", "AT", "CH"], "pnw": ["US-WA"]})
    assert tier("Vienna, Austria", cfg) == (0, "DACH")
    assert tier("Seattle, WA", cfg) == (1, "PNW")
    assert tier("Portland, OR", cfg) == (None, "")


def test_outside_is_a_company_placed_in_no_entry():
    cfg = config(geography=PNW, coords={})
    assert outside("HK", cfg)
    assert outside("Lyon, France", cfg)
    assert outside("Sydney, Australia", cfg)
    assert not outside("Seattle, WA", cfg)
    assert not outside("Toronto, ON", cfg)
    # Unknown location: kept, ranked last.
    assert not outside("", cfg)
    assert not outside("Remote", cfg)


def test_outside_respects_last_and_unplaceable_entries():
    assert not outside("HK", config(geography=PNW, coords={}, outside_geography="last"))
    # Sellbird can't tell who is in "Western Europe", so it rules nobody out.
    assert not outside("HK", config(geography=("United States", "Western Europe"), coords={}))
    assert not outside("HK", config(geography=(), coords={}))


def test_writer_leaves_out_of_territory_prospects_unwritten():
    writer = Writer.__new__(Writer)
    writer.config = config(geography=PNW, coords={})
    writer.state = FakeState({"hk": "HK", "wa": "Washougal, WA", "fr": "FR", "us": "Austin, TX"})
    p = lambda pid, cid: SimpleNamespace(id=pid, company_id=cid, score=50)
    kept = asyncio.run(writer._in_territory([p("1", "hk"), p("2", "wa"), p("3", "fr"),
                                             p("4", "us"), p("5", "")]))
    assert [x.id for x in kept] == ["2", "4", "5"]


# ── placing a person: their own location first, then the company's footprint ──

def test_person_location_wins_over_the_company():
    cfg = config(geography=PNW, coords={})
    # A Pittsburgh VP at a company with a Bellevue office is in the US tier.
    p = place(cfg, ("Pittsburgh, PA", ""), [("Chicago, IL", ""), ("Bellevue, WA", "")])
    assert (p.tier, p.basis, p.outside) == (1, PERSON, False)
    # A remote engineer in Seattle at an Austin company is local.
    assert place(cfg, ("Seattle, WA", ""), [("Austin, TX", "")]).tier == 0
    # Known to work in Hong Kong: out, even at a Seattle company.
    assert place(cfg, ("Hong Kong", ""), [("Seattle, WA", "")]).outside


def test_unplaceable_person_location_falls_back_to_the_company():
    cfg = config(geography=PNW, coords={})
    p = place(cfg, ("Remote", ""), [("Seattle, WA", "")])
    assert (p.tier, p.basis) == (0, COMPANY)


def test_company_footprint_is_a_weaker_guess_with_more_sites():
    cfg = config(geography=PNW, coords={})
    one = place(cfg, None, [("Seattle, WA", "")])
    many = place(cfg, None, [("Chicago, IL", ""), ("Bellevue, WA", ""), ("London, UK", "")])
    assert (one.tier, one.basis) == (0, COMPANY)
    assert (many.tier, many.basis, many.where) == (0, PRESENCE, "Bellevue, WA")
    known = place(cfg, ("Seattle, WA", ""), [])
    assert known.key(3) < one.key(3) < many.key(3)
    # Still behind nobody in a lower tier: a weak PNW guess beats a sure US one.
    assert many.key(3) < place(cfg, ("Austin, TX", ""), []).key(3)


def test_a_company_is_out_only_when_every_site_is():
    cfg = config(geography=PNW, coords={})
    assert place(cfg, None, [("Hong Kong", ""), ("Lyon, France", "")]).outside
    hk_with_seattle = place(cfg, None, [("Hong Kong", ""), ("Seattle, WA", "")])
    assert not hk_with_seattle.outside and hk_with_seattle.tier == 0
    assert not place(cfg, None, [("Hong Kong", ""), ("Remote", "")]).outside
    nothing = place(cfg, None, [])
    assert (nothing.tier, nothing.basis, nothing.outside) == (None, UNKNOWN, False)


def test_prospect_place_reads_people_and_sites():
    cfg = config(geography=PNW, coords={})
    state = FakeState({"coal": "Chicago, IL"}, sites={"coal": ["Bellevue, WA, US"]},
                      people={"john": [("Pittsburgh, PA", 0.9, "2026-11-01"),
                                       ("Seattle, WA", 0.5, "2026-11-08")]})
    john = SimpleNamespace(id="john", company_id="coal")
    other = SimpleNamespace(id="ann", company_id="coal")
    # The more confident source wins, even when a weaker one is newer.
    assert asyncio.run(prospect_place(state, john, cfg)).where == "Pittsburgh, PA"
    ann = asyncio.run(prospect_place(state, other, cfg))
    assert (ann.tier, ann.basis) == (0, PRESENCE)


def test_fit_counts_a_local_office():
    company = {"id": "c1", "domain": "acme.example", "industry": "", "location": "Chicago, IL"}
    sites = [{"signal_code": "COMPANY_SITE", "value_text": "Chicago, IL, US | Bellevue, WA, US",
              "observed_at": "2026-11-09"}]
    reasons = company_fit(company, [], sites, config(geography=PNW, coords={}))[1]
    assert "location: Pacific Northwest (priority 1): company has a site in Bellevue, WA, US" in reasons
