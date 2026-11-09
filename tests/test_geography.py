"""icp.geography is the territory in priority order: earlier entries are
written to first, and a company placed outside every entry is not contacted."""

import asyncio
from types import SimpleNamespace

from sellbird.agents.writer import Writer
from sellbird.fit import company_fit
from sellbird.geography import matches, outside, rank, tier


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
    assert "location: Canada (priority 3)" in reasons
    far = {**company, "location": "Lyon, France"}
    assert company_fit(far, [], [], config())[1][-1] == "location: outside your geography (not contacted)"
    kept = config(outside_geography="last")
    assert company_fit(far, [], [], kept)[1][-1] == "location: outside your geography (contacted last)"
    assert not any(r.startswith("location") for r in company_fit({**company, "location": ""}, [], [], config())[1])


class FakeState:
    def __init__(self, places):
        self.places = places

    async def get_company(self, company_id):
        return SimpleNamespace(location=self.places.get(company_id, ""))

    async def get_setting(self, key):
        return None


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
