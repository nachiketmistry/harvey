"""Who must not get cold email, and why.

One place for the rules that take a person out of cold outreach, read by
the Writer (nothing is drafted), the Sender's staging (nothing is queued),
its sweep (queued email is cancelled) and its last check before sending.
Each rule returns a reason or "", and the first reason wins:

1. What the company is to you (``company_relationships``): a competitor,
   customer or partner is never cold-emailed. This is the CRM account type
   (Salesforce's Account Type and its peers); only a prospect gets outreach.
2. Segment (``sellbird.segments``): a company in a segment set to "never"
   or "later", or clearly outside its segment's size bounds.
3. Geography: a person placed outside ``icp.geography`` (see
   ``sellbird.geography``).

A new rule is one more function in RULES.
"""

from __future__ import annotations

from sellbird.geography import OUTSIDE_GEOGRAPHY, prospect_outside

PROSPECT = "prospect"
# What a company can be to you, in the order the dashboard offers them.
RELATIONSHIPS = (PROSPECT, "competitor", "customer", "partner")
RELATIONSHIP_LABELS = {
    PROSPECT: "Prospect", "competitor": "Competitor",
    "customer": "Customer", "partner": "Partner",
}


def relationship_reason(relationship: str) -> str:
    """Why a company with this relationship gets no cold email, or ""."""
    if not relationship or relationship == PROSPECT:
        return ""
    return f"marked as a {relationship}"


async def _relationship(state, prospect, config) -> str:
    rel = await state.get_company_relationship(getattr(prospect, "company_id", "") or "")
    return relationship_reason(rel["relationship"]) if rel else ""


async def _geography(state, prospect, config) -> str:
    where = await prospect_outside(state, prospect, config)
    return f"{OUTSIDE_GEOGRAPHY} ({where})" if where else ""


async def _segment(state, prospect, config) -> str:
    from sellbird.segments import exclusion
    return await exclusion(state, getattr(prospect, "company_id", "") or "", config)


RULES = (_relationship, _segment, _geography)


async def excluded(state, prospect, config) -> str:
    """Why this person must get no cold email, or "" when they may."""
    for rule in RULES:
        reason = await rule(state, prospect, config)
        if reason:
            return reason
    return ""
