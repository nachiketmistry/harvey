"""`sellbird contacts`: record where people work, so the geography places
them by it (sellbird.geography.place)."""

import asyncio
from types import SimpleNamespace

import pytest

import sellbird.config as config_mod
import sellbird.state as state_mod
from sellbird.cli import cmd_contacts
from sellbird.locations import Place, row_place
from sellbird.geography import person_location
from sellbird.models import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

CFG = SimpleNamespace(icp=SimpleNamespace(
    geography=["Pacific Northwest", "United States", "Americas"],
    geo_coordinates={}, regions={}, outside_geography="skip"))


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "sellbird.db")
    monkeypatch.setattr(state_mod, "StateManager", lambda: StateManager(path))
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: CFG)

    async def seed():
        s = StateManager(path)
        await s.init_db()
        cid = await s.add_company(Company(name="Coalfire", domain="firm.example",
                                          location="Chicago, IL"))
        return s, await s.add_prospect(Prospect(
            first_name="John", last_name="Doe", company_id=cid, email="john@firm.example"))
    return asyncio.run(seed())


def test_locate_resolves_and_reports_the_tier(db, capsys, monkeypatch):
    state, pid = db
    # "Pittsburgh, PA" needs the geocoder (PA is Panama too); fake its answer.
    async def geocode(text, client=None):
        return Place("Pittsburgh", "US-PA", "US", 40.44, -79.99)
    monkeypatch.setattr("sellbird.locations.geocode_place", geocode)
    cmd_contacts(SimpleNamespace(locate=["John@Firm.example", "Pittsburgh, PA"], import_locations=""))
    assert "United States (priority 2): works in Pittsburgh, PA, US" in capsys.readouterr().out
    assert asyncio.run(person_location(state, pid)).region == "US-PA"


def test_a_bare_ambiguous_code_is_kept_as_text_that_needs_you(db, capsys):
    state, pid = db
    cmd_contacts(SimpleNamespace(locate=["john@firm.example", "DE"], import_locations=""))
    # Chicago, IL (the company) is ambiguous too: IL is Israel's code.
    assert 'location unknown: "DE; Chicago, IL" needs you' in capsys.readouterr().out
    assert asyncio.run(person_location(state, pid)) == "DE"


def test_your_location_beats_the_crm(db, tmp_path):
    state, pid = db
    csv = tmp_path / "crm.csv"
    csv.write_text("Email,City,State,Country\njohn@firm.example,Seattle,WA,US\n"
                   "nobody@else.example,Portland,OR,US\n,,,\n")
    cmd_contacts(SimpleNamespace(locate=["john@firm.example", "Austin, TX"], import_locations=""))
    cmd_contacts(SimpleNamespace(locate=None, import_locations=str(csv)))
    # The CRM row is newer, but what you typed is trusted more.
    assert asyncio.run(person_location(state, pid)) == Place("Austin", "US-TX", "US")


def test_csv_columns_map_field_by_field():
    # A country column's "DE" is Germany; a state column's "ID" is Idaho.
    assert row_place({"City": "Berlin", "Country": "DE"}) == (Place("Berlin", "", "DE"), "")
    assert row_place({"city": "Boise", "state": "ID", "country": "US"}) == (Place("Boise", "US-ID", "US"), "")
    assert row_place({"Location": "Seattle, WA"}) == (None, "Seattle, WA")
    assert row_place({"email": "x@y.example"}) == (None, "")


def test_company_locate_supersedes_the_free_text_field(db, capsys):
    from sellbird.cli import cmd_companies
    state, _ = db
    cmd_companies(SimpleNamespace(mark=None, locate=["firm.example", "Berlin, Germany"],
                                  import_locations="", out=""))
    assert "outside your geography (Berlin, DE)" in capsys.readouterr().out


def test_resolve_text_uses_the_geocoder_only_for_what_the_tables_cant_settle(db):
    from sellbird.locations import UNRESOLVED, resolve_text
    state, _ = db
    asked = []

    async def geocode(text):
        asked.append(text)
        return {"Pittsburgh, PA": Place("Pittsburgh", "US-PA", "US"),
                "Springfield, XX": Place("Springfield", "", "GB")}.get(text)

    async def go():
        assert (await resolve_text(state, "Seattle, WA", geocode))[0].region == "US-WA"
        assert (await resolve_text(state, "Pittsburgh, PA", geocode))[0].country == "US"
        # A bare code that's a country and a state too never reaches the geocoder.
        assert await resolve_text(state, "DE", geocode) == (None, True)
        assert await state.get_setting("place:de") == UNRESOLVED
        # Cached: asked once, ever.
        await resolve_text(state, "Pittsburgh, PA", geocode)
    asyncio.run(go())
    assert asked == ["Pittsburgh, PA"]
