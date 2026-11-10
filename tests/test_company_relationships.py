"""What a company is to you: only a prospect gets cold email.

A competitor is watched, a customer or partner left alone: none is drafted
for, staged, or sent to, and email already queued is cancelled. This is the
CRM account type (Salesforce's Account Type and its peers).
"""

import asyncio
from types import SimpleNamespace

import pytest

from sellbird.eligibility import excluded
from sellbird.models import Company
from sellbird.models.prospect import Prospect
from sellbird.register import build_register, sqlite_query
from sellbird.state import StateManager
from tests.test_outbox_native import FakeProvider, _now_iso, make_sender  # noqa: F401
from tests.test_outbox_review import client  # noqa: F401  (fixture)

CFG = SimpleNamespace(icp=SimpleNamespace(
    geography=["Pacific Northwest", "United States"], geo_coordinates={}, regions={},
    outside_geography="skip", industries=[], titles=[], hiring_signals=[], site_mentions=[]),
    compliance=SimpleNamespace(do_not_contact=[]))


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "sellbird.db"))
    asyncio.run(s.init_db())
    return s


async def _company(state, domain="rival.example", location="Seattle, WA"):
    cid = await state.add_company(Company(name="Rival", domain=domain, location=location))
    pid = await state.add_prospect(Prospect(
        first_name="Ann", last_name="Doe", company_id=cid, email=f"ann@{domain}",
        email_status="verified", status="new"))
    return cid, pid


def test_only_a_prospect_may_get_cold_email(state):
    async def go():
        cid, pid = await _company(state)
        p = await state.get_prospect(pid)
        assert await excluded(state, p, CFG) == ""
        await state.set_company_relationship(cid, "competitor", "builds a Splunk assistant")
        assert await excluded(state, p, CFG) == "marked as a competitor"
        await state.set_company_relationship(cid, "prospect")
        assert await state.get_company_relationship(cid) is None
        assert await excluded(state, p, CFG) == ""
    asyncio.run(go())


def test_relationship_comes_before_geography(state):
    async def go():
        cid, pid = await _company(state, location="Berlin, Germany")
        p = await state.get_prospect(pid)
        assert (await excluded(state, p, CFG)).startswith("outside your geography")
        await state.set_company_relationship(cid, "customer")
        assert await excluded(state, p, CFG) == "marked as a customer"
    asyncio.run(go())


def test_sender_cancels_email_queued_for_a_competitor(state):
    async def go():
        cid, pid = await _company(state)
        item = await state.add_outbox_item(
            prospect_id=pid, to_email="ann@rival.example", subject="s", body="b",
            send_at=_now_iso(), status="pending_review", campaign_id="c1", step=1, kind="sequence")
        sender = make_sender(state, FakeProvider())
        sender.config.icp = CFG.icp
        assert await sender._cancel_excluded() == 0
        await state.set_company_relationship(cid, "competitor")
        assert await sender._cancel_excluded() == 1
        row = await state.get_outbox_item(item)
        assert (row["status"], row["error"]) == ("cancelled", "marked as a competitor")
    asyncio.run(go())


def test_register_shows_the_relationship(state):
    async def go():
        cid, _ = await _company(state)
        await state.set_company_relationship(cid, "competitor", "builds a Splunk assistant")
        [row] = await build_register(sqlite_query(state.db_path), CFG)
        assert row["status"] == "Competitor" and row["relationship"] == "competitor"
        assert row["why_not_contacted"] == ("A competitor: watched, never cold-emailed. "
                                            "builds a Splunk assistant")
    asyncio.run(go())


def test_dashboard_sets_and_clears_a_relationship(client):  # noqa: F811
    async def seed():
        s = StateManager(str(client.db_path))
        await s.init_db()
        return s, (await _company(s))[0]
    s, cid = asyncio.run(seed())
    r = client.post(f"/api/companies/{cid}/relationship", json={"relationship": "Competitor", "note": "rival"})
    assert r.json() == {"success": True}
    assert asyncio.run(s.get_company_relationship(cid))["relationship"] == "competitor"
    [c] = client.get("/api/companies").json()
    assert c["relationship"] == "competitor" and c["register_status"] == "Competitor"
    assert client.post(f"/api/companies/{cid}/relationship", json={"relationship": "enemy"}).status_code == 400
    assert client.post("/api/companies/nope/relationship", json={"relationship": "partner"}).status_code == 404
    client.post(f"/api/companies/{cid}/relationship", json={"relationship": "prospect"})
    assert asyncio.run(s.get_company_relationship(cid)) is None
