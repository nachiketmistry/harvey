"""The review queue: Sellbird drafts a small batch, you review it, and the
next batch is written with your edits in hand.

With approval on, the Writer drafts first emails only until
channels.email.max_awaiting_review people have one waiting. A draft that's
off can be redrafted (thrown away and written again, with an optional note),
and the owner's edits to earlier drafts go to the Writer as before/after
examples.
"""

import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest

from sellbird.agents.writer import Writer
from sellbird.models import Company
from sellbird.models.campaign import Campaign
from sellbird.models.prospect import Prospect
from sellbird.state import REDRAFTED, StateManager
from tests.test_outbox_review import client  # noqa: F401  (fixture)


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "sellbird.db"))
    asyncio.run(s.init_db())
    return s


async def _person(state, email, status="queued"):
    cid = await state.add_company(Company(name="Acme", domain=email.split("@")[1]))
    return await state.add_prospect(Prospect(
        first_name="Ann", last_name="Doe", title="IT Director", company="Acme",
        company_id=cid, email=email, email_status="verified", status=status))


async def _campaign(state, pids, status="active"):
    return await state.add_campaign(Campaign(
        id="", name="test", channel="email", sequence=[], prospect_ids=pids, status=status))


async def _draft(state, pid, cid, step=1, status="pending_review", subject="hi", body="text"):
    return await state.add_outbox_item(
        prospect_id=pid, campaign_id=cid, step=step, to_email="x@acme.example",
        subject=subject, body=body, send_at="2026-11-09T12:00:00", status=status)


async def _row(state, item_id):
    return await state.get_outbox_item(item_id)


# ── redraft ──


def test_redraft_throws_drafts_away_and_requeues_the_person(state):
    async def go():
        ann = await _person(state, "ann@acme.example")
        bob = await _person(state, "bob@other.example")
        cid = await _campaign(state, [ann, bob])
        first = await _draft(state, ann, cid, 1)
        second = await _draft(state, ann, cid, 2, status="approved")
        kept = await _draft(state, bob, cid, 1)
        await state.set_setting("write_attempts:" + ann, "2")
        assert await state.redraft_prospect(ann) == 2
        for item in (first, second):
            row = await _row(state, item)
            assert row["status"] == "cancelled" and row["error"] == REDRAFTED
        assert (await _row(state, kept))["status"] == "pending_review"
        assert (await state.get_prospect(ann)).status == "new"
        assert not await state.get_setting("write_attempts:" + ann)
        [campaign] = await state.get_campaigns_by_status("active")
        assert campaign.prospect_ids == [bob]
    asyncio.run(go())


def test_redraft_closes_a_campaign_left_empty(state):
    async def go():
        ann = await _person(state, "ann@acme.example")
        cid = await _campaign(state, [ann])
        await _draft(state, ann, cid)
        await state.redraft_prospect(ann)
        [campaign] = await state.get_campaigns_by_status("completed")
        assert campaign.id == cid and campaign.prospect_ids == []
    asyncio.run(go())


@pytest.mark.parametrize("sent_status", ["sent", "sending"])
def test_redraft_refuses_once_a_cold_email_has_gone(state, sent_status):
    async def go():
        ann = await _person(state, "ann@acme.example")
        cid = await _campaign(state, [ann])
        await _draft(state, ann, cid, 1, status=sent_status)
        follow_up = await _draft(state, ann, cid, 2)
        assert await state.redraft_prospect(ann) == 0
        assert (await _row(state, follow_up))["status"] == "pending_review"
        assert (await state.get_prospect(ann)).status == "queued"
    asyncio.run(go())


def test_redraft_leaves_people_who_moved_on_alone(state):
    async def go():
        ann = await _person(state, "ann@acme.example", status="replied")
        cid = await _campaign(state, [ann])
        item = await _draft(state, ann, cid)
        assert await state.redraft_prospect(ann) == 0
        assert (await _row(state, item))["status"] == "pending_review"
    asyncio.run(go())


def test_awaiting_review_counts_people_with_a_first_draft(state):
    async def go():
        ann = await _person(state, "ann@acme.example")
        bob = await _person(state, "bob@other.example")
        cid = await _campaign(state, [ann, bob])
        await _draft(state, ann, cid, 1)
        await _draft(state, ann, cid, 2)
        await _draft(state, bob, cid, 1, status="approved")
        assert await state.count_first_drafts_awaiting_review() == 1
        assert await state.prospects_awaiting_first_review() == [ann]
    asyncio.run(go())


# ── learning from edits ──


async def _edit(state, item_id, subject, body):
    old = await _row(state, item_id)
    await state.edit_outbox_draft(item_id, subject, body)
    await state.log_action("outbox_edited", "dashboard", {
        "outbox_id": item_id, "old_subject": old["subject"], "old_body": old["body"]})


def test_draft_edits_pair_the_writers_draft_with_the_final_text(state):
    async def go():
        ann = await _person(state, "ann@acme.example")
        cid = await _campaign(state, [ann])
        item = await _draft(state, ann, cid, subject="who owns splunk", body="Who on your team?")
        await _edit(state, item, "splunk license check", "I'm Nachiket. Useful?")
        await _edit(state, item, "splunk license check", "I'm Nachiket, founder. Useful?")
        unchanged = await _draft(state, ann, cid, step=2, subject="s", body="b")
        await _edit(state, unchanged, "s", "b")
        [edit] = await state.get_draft_edits()
        assert edit["before_subject"] == "who owns splunk"
        assert edit["before_body"] == "Who on your team?"
        assert edit["after_body"] == "I'm Nachiket, founder. Useful?"
    asyncio.run(go())


def test_draft_edits_skip_rejected_drafts(state):
    async def go():
        ann = await _person(state, "ann@acme.example")
        cid = await _campaign(state, [ann])
        item = await _draft(state, ann, cid)
        await _edit(state, item, "new", "new body")
        await state.reject_outbox_item(item)
        assert await state.get_draft_edits() == []
    asyncio.run(go())


# ── the Writer ──


def _writer(limit=10, approval=True, awaiting=0, edits=()):
    w = Writer.__new__(Writer)
    w.config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
        max_awaiting_review=limit, require_approval=approval)))

    class S:
        async def count_first_drafts_awaiting_review(self):
            return awaiting

        async def get_draft_edits(self, limit=5):
            return list(edits)
    w.state = S()
    return w


@pytest.mark.parametrize("limit,approval,awaiting,room", [
    (10, True, 0, 10), (10, True, 7, 3), (10, True, 12, -2),
    (0, True, 50, None), (10, False, 50, None),
])
def test_review_room(limit, approval, awaiting, room):
    assert asyncio.run(_writer(limit, approval, awaiting)._review_room()) == room


def test_edits_block_shows_before_and_after():
    w = _writer(edits=[{"before_subject": "who owns splunk", "before_body": "Who on your team?",
                        "after_subject": "splunk license check", "after_body": "I'm Nachiket."}])
    block = asyncio.run(w._edits_block())
    assert "OWNER EDITS" in block
    assert block.index("Who on your team?") < block.index("I'm Nachiket.")
    assert asyncio.run(_writer()._edits_block()) == ""


# ── the dashboard ──


async def _seed_dash(db_path, sent=False):
    s = StateManager(str(db_path))
    await s.init_db()
    ann = await _person(s, "ann@acme.example")
    cid = await _campaign(s, [ann])
    if sent:
        await _draft(s, ann, cid, 1, status="sent")
    item = await _draft(s, ann, cid, 2 if sent else 1)
    return s, ann, item


def test_dashboard_redraft_keeps_the_note_for_that_person(client):  # noqa: F811
    s, ann, item = asyncio.run(_seed_dash(client.db_path))
    r = client.post(f"/api/outbox/{item}/redraft", json={"note": "Shorter, lead with renewal"})
    assert r.json() == {"success": True, "drafts": 1}
    assert asyncio.run(s.get_outbox_item(item))["status"] == "cancelled"
    notes = asyncio.run(s.get_writing_feedback([ann]))
    assert [n["comment"] for n in notes] == ["Shorter, lead with renewal"]


def test_dashboard_redraft_refuses_after_a_send(client):  # noqa: F811
    s, _, item = asyncio.run(_seed_dash(client.db_path, sent=True))
    r = client.post(f"/api/outbox/{item}/redraft", json={})
    assert r.status_code >= 400 and not r.json()["success"]
    assert asyncio.run(s.get_outbox_item(item))["status"] == "pending_review"


def test_outbox_reports_the_true_totals_beyond_the_first_page(client):  # noqa: F811
    async def seed():
        s = StateManager(str(client.db_path))
        await s.init_db()
        cid = await _campaign(s, [])
        for n in range(105):
            pid = await _person(s, f"p{n}@acme{n}.example")
            await _draft(s, pid, cid)
    asyncio.run(seed())
    data = client.get("/api/outbox").json()
    assert len(data["pending"]) == 100
    assert data["pending_total"] == 105 and data["held_total"] == 0
