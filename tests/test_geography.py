"""icp.geography is the territory in priority order: earlier entries are
written to first, and a person placed outside every entry is not contacted.

Matching works on structured places (city, ISO 3166-2 region, ISO 3166-1
country). Free text the reference tables can't settle ("Denver, CO": CO is
Colombia too) is unknown until the geocoder or you resolve it.
"""

import asyncio
from types import SimpleNamespace

from sellbird.agents.writer import Writer
from sellbird.fit import company_fit
from sellbird.geography import (
    COMPANY, PERSON, PRESENCE, UNKNOWN, matches, outside, place, prospect_place, rank, tier,
    tier_place,
)
from sellbird.locations import Place, places_value

PNW = ("Pacific Northwest", "United States", "Americas")


def config(geography=("Denver, CO", "United States", "Canada"), coords=None,
           regions=None, outside_geography="skip"):
    return SimpleNamespace(icp=SimpleNamespace(
        geography=list(geography),
        geo_coordinates=coords if coords is not None else {"Denver, CO": "39.7392,-104.9903,50"},
        regions=regions or {}, outside_geography=outside_geography,
        industries=[], titles=[], hiring_signals=[],
    ), compliance=SimpleNamespace(do_not_contact=[]))


def P(city="", region="", country="", lat=None, lng=None):
    return Place(city=city, region=region, country=country, lat=lat, lng=lng)


# ── entries ──


def test_your_entries_read_a_code_after_a_city_as_the_state():
    # You wrote "Denver, CO" about your own territory: Colorado, not Colombia.
    assert tier_place(P("Denver", "US-CO", "US"), config()) == (0, "Denver, CO")
    assert tier_place(P("Aurora", "US-CO", "US"), config()) == (1, "United States")


def test_city_entry_matches_inside_its_radius():
    near = P("Lakewood", "US-CO", "US", 39.7047, -105.0814)
    far = P("Pueblo", "US-CO", "US", 38.2544, -104.6091)
    assert tier_place(near, config()) == (0, "Denver, CO")
    assert tier_place(far, config()) == (1, "United States")


def test_country_entries_match_by_country():
    assert tier("Austin, TX", config()) == (1, "United States")
    assert tier("Toronto, ON", config()) == (2, "Canada")
    assert tier("Canada", config()) == (2, "Canada")


def test_same_city_name_in_another_country_does_not_match():
    assert not matches("London, UK", "London, ON, Canada")
    assert matches("London, UK", "London, United Kingdom")


def test_outside_every_entry_ranks_last():
    cfg = config()
    assert tier("Lyon, France", cfg) == (None, "")
    assert rank("Lyon, France", cfg) == 3 and rank("", cfg) == 3
    assert rank("Austin, TX", cfg) < rank("Toronto, ON", cfg) < rank("Lyon, France", cfg)


def test_no_geography_ranks_everyone_equal():
    cfg = config(geography=(), coords={})
    assert rank("Austin, TX", cfg) == rank("Lyon, France", cfg) == 0


def test_built_in_regions_match_by_state_and_country():
    cfg = config(geography=PNW, coords={})
    assert tier_place(P("Washougal", "US-WA", "US"), cfg) == (0, "Pacific Northwest")
    assert tier_place(P("Boise", "US-ID", "US"), cfg) == (0, "Pacific Northwest")
    assert tier("Portland, Oregon", cfg) == (0, "Pacific Northwest")
    assert tier("Vancouver, BC", cfg) == (0, "Pacific Northwest")
    assert tier("Oakland, CA, USA", cfg) == (1, "United States")
    assert tier("Toronto, ON", cfg) == (2, "Americas")
    assert tier("Sao Paulo, Brazil", cfg) == (2, "Americas")
    assert tier("Hong Kong", cfg) == (None, "")
    # Perth, Western Australia is not Washington state.
    assert tier("Perth, WA, Australia", cfg) == (None, "")


def test_configured_regions_add_to_and_override_built_ins():
    cfg = config(geography=("DACH", "PNW"), coords={},
                 regions={"DACH": ["DE", "AT", "CH"], "pnw": ["US-WA"]})
    assert tier("Vienna, Austria", cfg) == (0, "DACH")
    assert tier("Seattle, WA", cfg) == (1, "PNW")
    assert tier("Portland, Oregon", cfg) == (None, "")


# ── ambiguity is unknown, never a guess ──


def test_ambiguous_text_is_unknown_not_guessed():
    cfg = config(geography=PNW, coords={})
    for text in ("DE", "IN", "CA", "Pittsburgh, PA", "Boise, ID"):
        assert tier(text, cfg) == (None, "") and not outside(text, cfg), text
    assert outside("Berlin, Germany", cfg) and outside("Hong Kong", cfg)


def test_outside_respects_last_and_unplaceable_entries():
    assert not outside("Hong Kong", config(geography=PNW, coords={}, outside_geography="last"))
    # Sellbird can't tell who is in "Western Europe", so it rules nobody out.
    assert not outside("Hong Kong", config(geography=("United States", "Western Europe"), coords={}))
    assert not outside("Hong Kong", config(geography=(), coords={}))


# ── placing a person: their own location first, then the company's footprint ──


def test_person_location_wins_over_the_company():
    cfg = config(geography=PNW, coords={})
    pittsburgh = P("Pittsburgh", "US-PA", "US")
    p = place(cfg, pittsburgh, [P("Chicago", "US-IL", "US"), P("Bellevue", "US-WA", "US")])
    assert (p.tier, p.basis, p.outside) == (1, PERSON, False)
    assert place(cfg, P("Seattle", "US-WA", "US"), [P("Austin", "US-TX", "US")]).tier == 0
    assert place(cfg, P(country="HK"), [P("Seattle", "US-WA", "US")]).outside


def test_unknown_person_falls_back_to_the_company():
    cfg = config(geography=PNW, coords={})
    p = place(cfg, P(city="Remote"), [P("Seattle", "US-WA", "US")])
    assert (p.tier, p.basis) == (0, COMPANY)


def test_company_footprint_is_a_weaker_guess_with_more_sites():
    cfg = config(geography=PNW, coords={})
    one = place(cfg, None, [P("Seattle", "US-WA", "US")])
    many = place(cfg, None, [P("Chicago", "US-IL", "US"), P("Bellevue", "US-WA", "US"),
                             P("London", "", "GB")])
    assert (one.tier, one.basis) == (0, COMPANY)
    assert (many.tier, many.basis, many.where) == (0, PRESENCE, "Bellevue, WA, US")
    known = place(cfg, P("Seattle", "US-WA", "US"), [])
    assert known.key(3) < one.key(3) < many.key(3)
    # A weak local guess still beats someone known to be in a lower tier.
    assert many.key(3) < place(cfg, P("Austin", "US-TX", "US"), []).key(3)


def test_a_company_is_out_only_when_every_site_is_known_and_out():
    cfg = config(geography=PNW, coords={})
    assert place(cfg, None, [P(country="HK"), P("Lyon", "", "FR")]).outside
    assert not place(cfg, None, [P(country="HK"), P("Seattle", "US-WA", "US")]).outside
    # An unresolved site might be inside: not out, and it says what needs you.
    maybe = place(cfg, None, [P(country="HK")], unresolved=("DE",))
    assert not maybe.outside and maybe.basis == PRESENCE
    nothing = place(cfg, None, [], unresolved=("DE",))
    assert (nothing.tier, nothing.basis, nothing.outside) == (None, UNKNOWN, False)
    assert nothing.reason() == 'location unknown: "DE" needs you'


class FakeState:
    """Companies by id with one location each; offices per company
    (COMPANY_SITE), locations per person (PERSON_LOCATION), and settings
    (the place: cache)."""
    def __init__(self, places, sites=None, people=None, settings=None):
        self.places, self.sites, self.people = places, sites or {}, people or {}
        self.settings = settings or {}

    async def get_company(self, company_id):
        return SimpleNamespace(location=self.places.get(company_id, ""))

    async def get_setting(self, key):
        return self.settings.get(key, "")

    async def set_setting(self, key, value):
        self.settings[key] = value

    async def get_company_relationship(self, company_id):
        return None

    async def get_observations(self, company_id="", signal_code="", latest_only=False, limit=500):
        sites = self.sites.get(company_id)
        return ([{"signal_code": "COMPANY_SITE", "collector": "profile",
                  "value_text": places_value(sites), "observed_at": "2026-11-09"}]
                if sites and signal_code == "COMPANY_SITE" else [])

    async def get_prospect_observations(self, prospect_id, signal_code):
        return [{"value_text": v, "confidence": c, "observed_at": t}
                for v, c, t in self.people.get(prospect_id, [])] if signal_code == "PERSON_LOCATION" else []


def test_prospect_place_reads_people_sites_and_the_cache():
    cfg = config(geography=PNW, coords={})
    state = FakeState({"coal": "Chicago, IL (HQ)", "rival": "DE"},
                      sites={"coal": [P("Bellevue", "US-WA", "US")]},
                      people={"john": [(P("Pittsburgh", "US-PA", "US").to_json(), 0.9, "2026-11-01"),
                                       (P("Seattle", "US-WA", "US").to_json(), 0.5, "2026-11-08")]},
                      settings={"place:chicago, il (hq)": P("Chicago", "US-IL", "US").to_json()})
    john = SimpleNamespace(id="john", company_id="coal")
    # The more confident source wins, even when a weaker one is newer.
    assert asyncio.run(prospect_place(state, john, cfg)).where == "Pittsburgh, PA, US"
    ann = asyncio.run(prospect_place(state, SimpleNamespace(id="ann", company_id="coal"), cfg))
    assert (ann.tier, ann.basis) == (0, PRESENCE)
    rival = asyncio.run(prospect_place(state, SimpleNamespace(id="x", company_id="rival"), cfg))
    assert (rival.tier, rival.outside, rival.unresolved) == (None, False, ("DE",))


def test_writer_orders_by_tier_then_score():
    writer = Writer.__new__(Writer)
    writer.config = config(geography=PNW, coords={})
    writer.state = FakeState({"far": "Lyon, France", "us": "Austin, TX", "home": "Seattle, WA",
                              "ca": "Toronto, ON"})
    p = lambda pid, cid, score: SimpleNamespace(id=pid, company_id=cid, score=score)
    prospects = [p("1", "far", 99), p("2", "us", 40), p("3", "home", 10), p("4", "us", 80),
                 p("5", "ca", 90), p("6", "", 95)]
    ordered = asyncio.run(writer._prioritise(prospects))
    assert [x.id for x in ordered][:4] == ["3", "4", "2", "5"]


def test_writer_leaves_out_of_territory_prospects_unwritten():
    writer = Writer.__new__(Writer)
    writer.config = config(geography=PNW, coords={})
    writer.state = FakeState({"hk": "Hong Kong", "wa": "Seattle, WA", "fr": "Lyon, France",
                              "us": "Austin, TX", "de": "DE"})
    p = lambda pid, cid: SimpleNamespace(id=pid, company_id=cid, score=50)
    kept = asyncio.run(writer._eligible([p("1", "hk"), p("2", "wa"), p("3", "fr"),
                                         p("4", "us"), p("5", ""), p("6", "de")]))
    assert [x.id for x in kept] == ["2", "4", "5", "6"]


# ── fit ──


def test_fit_reports_location_without_changing_the_score():
    company = {"id": "c1", "domain": "acme.example", "industry": "", "location": "Toronto, ON"}
    score, reasons = company_fit(company, [], [], config())
    assert score == 0
    assert "location: Canada (priority 3): company is in Toronto, ON, CA" in reasons
    far = {**company, "location": "Lyon, France"}
    assert company_fit(far, [], [], config())[1][-1] == "location: outside your geography (not contacted)"
    kept = config(outside_geography="last")
    assert company_fit(far, [], [], kept)[1][-1] == "location: outside your geography (contacted last)"
    assert not any(r.startswith("location") for r in company_fit({**company, "location": ""}, [], [], config())[1])


def test_fit_counts_a_local_office():
    company = {"id": "c1", "domain": "acme.example", "industry": "", "location": "Chicago, IL, USA"}
    sites = [{"signal_code": "COMPANY_SITE", "collector": "profile", "observed_at": "2026-11-09",
              "value_text": places_value([P("Chicago", "US-IL", "US"), P("Bellevue", "US-WA", "US")])}]
    reasons = company_fit(company, [], sites, config(geography=PNW, coords={}))[1]
    assert "location: Pacific Northwest (priority 1): company has a site in Bellevue, WA, US" in reasons
