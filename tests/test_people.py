"""People search: the right person at a company you already chose."""

from types import SimpleNamespace

import httpx
import pytest

from sellbird.integrations.email_finder import EmailResult
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.people import (
    SEARCHED, FoundPerson, PDLPeopleSearch, PeopleFinder, find_people, title_case,
)
from tests.test_outbox_native import state  # noqa: F401


class FakeFinder(PeopleFinder):
    key, label, env_key, default_cap = "fake", "Fake", "FAKE_KEY", 5

    def __init__(self, people):
        self.people, self.calls = people, []

    async def search(self, client, key, state, domain, titles, size):
        self.calls.append((domain, tuple(titles), size))
        found = self.people.get(domain, [])[:size]
        await self.spend(state, len(found))
        return found


ENV = SimpleNamespace(fake_key="k")


async def _company(state, domain):
    return await state.add_company(Company(name=domain.split(".")[0].title(), domain=domain))


def resolver(seen):
    async def find_email(first, last, domain, **kw):
        seen.append((first, kw.get("known_emails"), kw.get("other_people")))
        return EmailResult(email=f"{first.lower()}.{last.lower()}@{domain}", status="verified")
    return find_email


def test_the_pdl_query_cannot_be_widened_by_a_title():
    q = PDLPeopleSearch().query("acme.example", ["Splunk Admin", "O'Brien%_ Lead", "splunk admin"])
    assert q == ("SELECT * FROM person WHERE job_company_website = 'acme.example' AND "
                 "(job_title LIKE '%splunk admin%' OR job_title LIKE '%o''brien   lead%')")


@pytest.mark.asyncio
async def test_a_bad_domain_is_never_searched(state):
    async def handler(request):
        raise AssertionError("no request expected")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await PDLPeopleSearch().search(client, "k", state, "x' OR 1=1 --", ["SOC"], 2) == []


@pytest.mark.asyncio
async def test_pdl_counts_what_it_returns_and_what_it_says_is_left(state):
    finder = PDLPeopleSearch()
    responses = iter([
        httpx.Response(404, json={"error": {"type": "not_found"}},
                       headers={"x-totallimit-remaining": "40"}),
        httpx.Response(200, json={"data": [
            {"first_name": "dana", "last_name": "reyes", "job_title": "senior splunk engineer",
             "linkedin_url": "linkedin.com/in/example"},
            {"first_name": "", "last_name": "nobody", "job_title": "soc manager"},
        ]}, headers={"x-totallimit-remaining": "38"}),
    ])

    async def handler(request):
        return next(responses)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await finder.search(client, "k", state, "acme.example", ["Splunk"], 2) == []
        assert await finder.remaining(state) == 40          # a miss costs nothing
        found = await finder.search(client, "k", state, "acme.example", ["Splunk"], 2)
    assert found == [FoundPerson("Dana", "Reyes", "Senior Splunk Engineer", "linkedin.com/in/example")]
    assert await finder.remaining(state) == 38              # both records were billed


@pytest.mark.asyncio
async def test_found_people_join_the_company_with_an_address_from_its_pattern(state):
    cid = await _company(state, "acme.example")
    await state.add_prospect(Prospect(company_id=cid, first_name="Pat", last_name="Lee", title="CEO",
                                      email="pat.lee@acme.example", email_status="verified"))
    company = await state.get_company(cid)
    finder = FakeFinder({"acme.example": [FoundPerson("Pat", "Lee", "CEO"),
                                          FoundPerson("Dana", "Reyes", "Splunk Engineer")]})
    seen = []
    out = await find_people(state, ENV, [company], lambda c: ["Splunk Engineer", "CEO"],
                            per_company=2, finder=finder, find_email=resolver(seen))
    assert out.searched == 1 and out.already_known == 1
    [(name, added)] = out.added
    assert (added.first_name, added.email, added.email_status, added.source) == \
        ("Dana", "dana.reyes@acme.example", "verified", "fake_person_search")
    # The colleague on file teaches the pattern and is never this person's mailbox.
    assert seen == [("Dana", [("pat.lee@acme.example", "Pat", "Lee")], [("Pat", "Lee")])]
    assert await state.get_setting(f"{SEARCHED}fake:{cid}")

    again = await find_people(state, ENV, [company], lambda c: ["SOC"], finder=finder,
                              find_email=resolver([]))
    assert again.skipped_searched == 1 and len(finder.calls) == 1   # not paid for twice


@pytest.mark.asyncio
async def test_the_allowance_stops_the_run_and_no_titles_means_no_search(state):
    a = await state.get_company(await _company(state, "a.example"))
    b = await state.get_company(await _company(state, "b.example"))
    c = await state.get_company(await _company(state, "c.example"))
    finder = FakeFinder({"b.example": [FoundPerson(f"P{i}", "Q", "SOC Manager") for i in range(9)]})
    out = await find_people(state, ENV, [a, b, c], lambda co: [] if co is a else ["SOC Manager"],
                            per_company=9, finder=finder, find_email=resolver([]))
    assert out.skipped_no_titles == 1
    assert len(out.added) == 5 and out.out_of_allowance        # cap of 5 spent on b
    assert [call[0] for call in finder.calls] == ["b.example"]


@pytest.mark.parametrize("raw, shown", [
    ("senior it and security engineer ii", "Senior IT and Security Engineer II"),
    ("director of devops | head of platform", "Director of DevOps | Head of Platform"),
    ("soc manager, infosec", "SOC Manager, InfoSec"),
])
def test_titles_read_the_way_people_write_them(raw, shown):
    assert title_case(raw) == shown
