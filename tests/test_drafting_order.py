"""Who the Writer drafts first, and who goes in each review batch.

Within a geography tier, people are ordered the way lead scoring does it:
a title you target first, then how sure Sellbird is of where they are, the
company's fit, seniority, and how deliverable the address is. A batch
takes one person per company: a reply from anyone there stops everyone's
sequence, so colleagues wait for a later batch.
"""

from types import SimpleNamespace

import pytest

from sellbird.agents.writer import Writer, persona
from sellbird.collectors.people import title_matches
from sellbird.models.prospect import Prospect
from tests.test_outbox_native import Cfg, Env, state  # noqa: F401


@pytest.mark.parametrize("title, target", [
    ("Chief Information Security Officer", "CISO"),
    ("chief technology officer", "CTO"),
    ("Chief Executive Officer & Co-founder", "CEO"),
])
def test_a_spelled_out_c_suite_title_matches_its_abbreviation(title, target):
    assert title_matches(title, [target])


def test_a_spelled_out_title_doesnt_match_a_different_chief():
    assert not title_matches("Chief Information Officer", ["CISO"])
    assert not title_matches("Chief of Staff", ["CTO"])


def _p(first, title, company_id, status="verified", inbox=False):
    return Prospect(id=f"{first}-{company_id}", first_name=first, title=title,
                    company_id=company_id, email=f"{first.lower()}@{company_id}.example",
                    email_status=status, is_company_inbox=inbox)


def test_persona_ranks_target_titles_then_named_people_then_inboxes():
    cfg = Cfg()
    cfg.icp = SimpleNamespace(titles=["Founder", "Splunk Engineer"], geography=[])
    assert persona(_p("Ann", "Splunk Engineer", "a"), cfg) == 2
    assert persona(_p("Bo", "Founder & CEO", "a"), cfg) == 2
    assert persona(_p("Cy", "Office Manager", "a"), cfg) == 1
    assert persona(_p("", "General inbox", "a", inbox=True), cfg) == 0


@pytest.mark.asyncio
async def test_target_titles_and_seniority_lead_within_a_tier(state, monkeypatch):
    cfg = Cfg()
    cfg.icp = SimpleNamespace(titles=["Founder", "VP of Engineering"], geography=[])
    writer = Writer(None, state, cfg, Env())

    async def rank(*a, **k):
        return (0, 0)
    async def fits():
        return {"a": 70, "b": 90}
    monkeypatch.setattr("sellbird.agents.writer.prospect_rank", rank)
    monkeypatch.setattr(writer, "_company_fits", fits)
    people = [_p("Zed", "Systems Engineer", "b"), _p("Vic", "VP of Engineering", "a"),
              _p("Fay", "Founder & CEO", "a"), _p("Ola", "VP of Engineering", "b", "risky")]
    order = [p.first_name for p in await writer._prioritise(people)]
    # target titles first; the fitter company first; the founder before the VP
    assert order == ["Ola", "Fay", "Vic", "Zed"]


@pytest.mark.asyncio
async def test_a_batch_takes_one_person_per_company(state, monkeypatch):
    writer = Writer(None, state, Cfg(), Env())

    async def busy():
        return {"c"}
    monkeypatch.setattr(state, "companies_with_live_outreach", busy)
    queue = [_p("A1", "", "a"), _p("A2", "", "a"), _p("B1", "", "b"),
             _p("C1", "", "c"), _p("D1", "", "d"), _p("E1", "", "e")]
    picked = [p.first_name for p in await writer._batch(queue, 3)]
    assert picked == ["A1", "B1", "D1"]   # A2 is A1's colleague; C has email waiting


@pytest.mark.asyncio
async def test_companies_with_queued_cold_email_are_busy(state):
    from sellbird.models.company import Company
    co = await state.add_company(Company(name="Acme", domain="acme.example"))
    pid = await state.add_prospect(Prospect(first_name="Jo", email="jo@acme.example",
                                            email_status="verified", company_id=co))
    assert await state.companies_with_live_outreach() == set()
    await state.add_outbox_item(prospect_id=pid, to_email="jo@acme.example",
                                subject="s", body="b", send_at="2026-11-10T17:00:00+00:00")
    assert await state.companies_with_live_outreach() == {co}


@pytest.mark.asyncio
@pytest.mark.parametrize("send_to_risky, expected", [(False, ["Vera"]), (True, ["Vera", "Rita"])])
async def test_risky_addresses_are_drafted_only_when_they_would_be_sent(state, send_to_risky, expected):
    cfg = Cfg()
    cfg.channels.email.send_to_risky = send_to_risky
    for first, status in (("Vera", "verified"), ("Rita", "risky"), ("Gus", "guess")):
        await state.add_prospect(Prospect(first_name=first, email=f"{first.lower()}@x{first}.example",
                                          email_status=status))
    skipped = {}
    queue = await Writer(None, state, cfg, Env()).queue(skipped)
    assert sorted(p.first_name for p in queue) == sorted(expected)
    assert await state.count_writable_prospects(send_to_risky) == len(expected)
    if not send_to_risky:
        assert skipped["risky (catch-all) email, and channels.email.send_to_risky is off"] == 1
    assert skipped["no deliverable email (guessed, invalid or none)"] == 1
