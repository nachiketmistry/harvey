"""A company placed outside icp.geography gets no cold email: it isn't
staged, anything already queued for it is cancelled, and an approved email
is refused at send time."""

from types import SimpleNamespace

import pytest

from sellbird.geography import OUTSIDE_GEOGRAPHY
from sellbird.state import Company, Prospect
from tests.test_outbox_native import (  # noqa: F401  (fixture)
    FakeProvider, _now_iso, make_sender, seed_campaign, state,
)

TERRITORY = SimpleNamespace(
    geography=["Pacific Northwest", "United States", "Americas"],
    geo_coordinates={}, regions={}, outside_geography="skip",
)


def _sender(state):
    sender = make_sender(state, FakeProvider())
    sender.config.icp = TERRITORY
    return sender


async def _person(state, domain, location):
    cid = await state.add_company(Company(name=domain, domain=domain, location=location))
    return await state.add_prospect(Prospect(
        first_name="Ann", last_name="Doe", title="IT Director", company=domain,
        company_id=cid, email=f"ann@{domain}", email_status="verified",
        email_verified=True, status="new",
    ))


async def _queued(state, pid, email, status, step=1):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b",
        send_at=_now_iso(), status=status, campaign_id="c1", step=step, kind="sequence",
    )


async def _row(state, item_id):
    [row] = [r for r in await state.get_outbox() if r["id"] == item_id]
    return row


@pytest.mark.asyncio
async def test_staging_leaves_out_of_territory_prospects_out(state):
    hk = await _person(state, "campus.example.hk", "HK")
    wa = await _person(state, "plant.example.com", "Washougal, WA")
    nowhere = await _person(state, "startup.example.com", "")
    campaign = await seed_campaign(state, [hk, wa, nowhere])
    await _sender(state)._stage_campaign_native(campaign)
    staged = {r["prospect_id"] for r in await state.get_outbox()}
    assert staged == {wa, nowhere}


@pytest.mark.asyncio
async def test_sweep_cancels_queued_email_outside_territory(state):
    hk = await _person(state, "campus.example.hk", "HK")
    us = await _person(state, "shop.example.com", "Austin, TX")
    waiting = await _queued(state, hk, "ann@campus.example.hk", "pending_review")
    approved = await _queued(state, hk, "ann@campus.example.hk", "approved", step=2)
    kept = await _queued(state, us, "ann@shop.example.com", "pending_review")
    assert await _sender(state)._cancel_outside_geography() == 2
    for item in (waiting, approved):
        row = await _row(state, item)
        assert row["status"] == "cancelled" and OUTSIDE_GEOGRAPHY in row["error"]
        assert "HK" in row["error"]
    assert (await _row(state, kept))["status"] == "pending_review"


@pytest.mark.asyncio
async def test_send_time_check_refuses_out_of_territory(state):
    fr = await _person(state, "firm.example.fr", "Lyon, France")
    item = await _queued(state, fr, "ann@firm.example.fr", "approved")
    sender = _sender(state)
    verdict = await sender._blocked(await _row(state, item), await state.get_prospect(fr), False)
    assert verdict[0] == "cancelled" and OUTSIDE_GEOGRAPHY in verdict[1]


@pytest.mark.asyncio
async def test_last_keeps_out_of_territory(state):
    hk = await _person(state, "campus.example.hk", "HK")
    waiting = await _queued(state, hk, "ann@campus.example.hk", "pending_review")
    sender = _sender(state)
    sender.config.icp = SimpleNamespace(**{**vars(TERRITORY), "outside_geography": "last"})
    assert await sender._cancel_outside_geography() == 0
    assert (await _row(state, waiting))["status"] == "pending_review"
