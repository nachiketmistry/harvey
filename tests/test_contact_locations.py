"""`sellbird contacts`: record where people work, so the geography places
them by it (sellbird.geography.place)."""

import asyncio
from types import SimpleNamespace

import pytest

import sellbird.config as config_mod
import sellbird.state as state_mod
from sellbird.cli import _csv_location, cmd_contacts
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


def test_locate_records_and_reports_the_tier(db, capsys):
    state, pid = db
    cmd_contacts(SimpleNamespace(locate=["John@Firm.example", "Pittsburgh, PA"], import_locations=""))
    assert "United States (priority 2): works in Pittsburgh, PA" in capsys.readouterr().out
    assert asyncio.run(person_location(state, pid)) == "Pittsburgh, PA"


def test_your_location_beats_the_crm(db, tmp_path):
    state, pid = db
    csv = tmp_path / "crm.csv"
    csv.write_text("Email,City,State,Country\njohn@firm.example,Seattle,WA,US\n"
                   "nobody@else.example,Portland,OR,US\n,,,\n")
    cmd_contacts(SimpleNamespace(locate=["john@firm.example", "Pittsburgh, PA"], import_locations=""))
    cmd_contacts(SimpleNamespace(locate=None, import_locations=str(csv)))
    # The CRM row is newer, but what you typed is trusted more.
    assert asyncio.run(person_location(state, pid)) == "Pittsburgh, PA"


def test_csv_location_columns():
    assert _csv_location({"Location": "Seattle, WA"}) == "Seattle, WA"
    assert _csv_location({"city": "Boise", "state": "ID", "country": ""}) == "Boise, ID"
    assert _csv_location({"email": "x@y.example"}) == ""
