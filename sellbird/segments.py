"""Segments: the kinds of company you sell to, and which kind each company is.

One ICP usually holds several buyers that need different handling. Selling
a tool for engineers, an in-house team buys it, a five-person consultancy
might partner on it, and a global integrator only signs once its clients
ask. Sales teams model this as segments (account tiers in ABM, a sequence
per persona in Outreach or Salesloft): each has its own firmographic bounds,
its own play, and a decision about whether to pursue it now.

Here a segment is configuration (``icp.segments``, see
``sellbird.config.SegmentConfig``), and which segment a company is in is an
observation like every other fact:

- COMPANY_SEGMENT: ``{"segment": key, "reason": why}``. The classifier
  proposes one from what Sellbird holds about the company (collector
  ``classifier``); you confirm or change it (collector ``you``). The most
  trusted reading wins, so your word always beats a proposal, and the
  history stays on disk.
- EMPLOYEE_COUNT: headcount as a range ("1001-5000", "10001+"), from an
  enrichment provider, your CRM or you. The company's ``company_size``
  field, as imported, is the fallback.

What follows from a company's segment:

- Eligibility (``sellbird.eligibility``): a segment set to "never" is never
  written to, one set to "later" is parked, and a company clearly outside
  its segment's size bounds is held (a 10,001+ company in a segment capped
  at 2,000). Queued email to them is cancelled like any other exclusion.
- Order: earlier segments are drafted first (``Writer._prioritise``).
- Voice: the segment's playbook (``skills/<playbook>.md``) goes into the
  Writer's and handler's prompts, so each kind of buyer gets the offer that
  fits it, and the batch preview shows the mix before anything is drafted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.segments")

SEGMENT = "COMPANY_SEGMENT"
EMPLOYEES = "EMPLOYEE_COUNT"
CLASSIFIER = "classifier"
UNKNOWN = "unknown"
# How far each source is trusted; the most trusted reading wins, then the
# newest. You beat your CRM beats a provider beats the classifier.
TRUST = {"you": 1.0, "crm": 0.9, "pdl": 0.8, "hunter": 0.75, CLASSIFIER: 0.6}
CLASSIFY_BATCH = 15
DESCRIPTION_MAX = 500


# ── Headcount ──

@dataclass(frozen=True)
class Size:
    low: int
    high: int | None   # None: open-ended ("10,001+")
    source: str = ""

    def label(self) -> str:
        if self.high is None:
            return f"{self.low:,}+"
        if self.high == self.low:
            return f"{self.low:,}"
        return f"{self.low:,}-{self.high:,}"

    def stored(self) -> str:
        return f"{self.low}+" if self.high is None else f"{self.low}-{self.high}"


_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?(?![\w])")


def _number(digits: str, suffix: str | None) -> int:
    value = float(digits.replace(",", ""))
    return int(value * {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1))


def parse_employees(text) -> tuple[int, int | None] | None:
    """Headcount text as (low, high), high None when open-ended. Reads the
    forms providers and CRMs use: "1001-5000", "1,001–5,000", "10,001+",
    "1K-5K", "100K+", "30+ engineers", "250". None when there's no number."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return (int(text), int(text)) if text > 0 else None
    raw = str(text or "")
    found = list(_NUM.finditer(raw))
    if not found:
        return None
    first = found[0]
    low = _number(first.group(1), first.group(2))
    rest = raw[first.end():]
    if len(found) > 1 and re.match(r"\s*(?:-|–|—|to)\s*", rest, re.IGNORECASE):
        high = _number(found[1].group(1), found[1].group(2))
        return (min(low, high), max(low, high))
    if rest.lstrip().startswith("+"):
        return (low, None)
    return (low, low) if low > 0 else None


def size_from_text(text, source: str = "") -> Size | None:
    parsed = parse_employees(text)
    return Size(parsed[0], parsed[1], source) if parsed else None


def _best(rows: list[dict]) -> dict | None:
    """The most trusted reading, the newest among equals."""
    return max(rows, key=lambda r: (float(r.get("confidence") or 0),
                                    r.get("observed_at") or ""), default=None)


def size_from_rows(rows: list[dict], on_file: str = "") -> Size | None:
    """The headcount from a company's observation rows (any signals; only
    EMPLOYEE_COUNT is read), else ``on_file`` (its company_size)."""
    usable = [r for r in rows if r.get("signal_code", EMPLOYEES) == EMPLOYEES
              and parse_employees(r.get("value_text") or r.get("value_num"))]
    best = _best(usable)
    if best:
        return size_from_text(best.get("value_text") or best.get("value_num"), best.get("collector") or "")
    return size_from_text(on_file, "on file")


async def company_size(state, company_id: str, company=None) -> Size | None:
    """What Sellbird knows of a company's headcount: the most trusted
    EMPLOYEE_COUNT reading, else its company_size as imported."""
    if not company_id:
        return None
    try:
        rows = await state.get_observations(company_id=company_id, signal_code=EMPLOYEES)
    except Exception:
        rows = []
    found = size_from_rows(rows)
    if found is not None:
        return found
    if company is None:
        try:
            company = await state.get_company(company_id)
        except Exception:
            company = None
    raw = getattr(company, "company_size", "") if company is not None else ""
    return size_from_text(raw, "on file")


async def record_size(state, company_id: str, text, source: str) -> Size | None:
    """Store a headcount reading. None (and nothing stored) when the text
    holds no number."""
    size = size_from_text(text, source)
    if size is None or not company_id:
        return None
    await state.add_observation(EMPLOYEES, company_id=company_id, collector=source,
                                value_num=size.low, value_text=size.stored(),
                                confidence=TRUST.get(source, 0.7))
    return size


# ── Segments ──

def configured(config) -> list:
    return list(getattr(getattr(config, "icp", None), "segments", None) or [])


def by_key(config, key: str):
    return next((s for s in configured(config) if s.key == key), None)


def priority(config, key: str) -> int:
    """Where a segment sits in your order (0 first); unknown ones last."""
    keys = [s.key for s in configured(config)]
    return keys.index(key) if key in keys else len(keys)


@dataclass(frozen=True)
class Assignment:
    key: str             # a configured segment's key, or UNKNOWN
    source: str          # "you", "classifier", ...
    reason: str = ""

    @property
    def confirmed(self) -> bool:
        return self.source != CLASSIFIER

    def segment(self, config):
        return by_key(config, self.key)

    def label(self, config) -> str:
        seg = self.segment(config)
        name = seg.name if seg else "Unknown"
        return name if self.confirmed else f"{name} (proposed)"


def _reading(row: dict) -> tuple[str, str]:
    try:
        data = json.loads(row.get("value_text") or "{}")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("segment") or "").strip().lower(), str(data.get("reason") or "").strip()


def assignment_from_rows(rows: list[dict], config) -> Assignment | None:
    """The segment from a company's observation rows (any signals; only
    COMPANY_SEGMENT is read): the most trusted reading that names a segment
    you still have, or says unknown."""
    if not configured(config):
        return None
    keys = {s.key for s in configured(config)} | {UNKNOWN}
    usable = [r for r in rows if r.get("signal_code", SEGMENT) == SEGMENT and _reading(r)[0] in keys]
    best = _best(usable)
    if best is None:
        return None
    key, reason = _reading(best)
    return Assignment(key, best.get("collector") or "", reason)


def hold(assignment: Assignment | None, size: Size | None, config) -> tuple[str, str]:
    """(kind, detail) when a company's segment keeps it from cold email now:
    "never" or "later" with the segment's name, or "size" with the
    conflict. ("", "") when it may be written to."""
    seg = assignment.segment(config) if assignment else None
    if seg is None:
        return "", ""
    if seg.status in ("never", "later"):
        return seg.status, seg.name
    conflict = size_conflict(seg, size)
    return ("size", conflict) if conflict else ("", "")


async def company_segment(state, company_id: str, config) -> Assignment | None:
    """The segment a company is in: the most trusted COMPANY_SEGMENT reading
    that names a segment you still have (or says unknown). None when there
    is no reading, or no segments are configured."""
    if not company_id or not configured(config):
        return None
    try:
        rows = await state.get_observations(company_id=company_id, signal_code=SEGMENT)
    except Exception:
        return None
    return assignment_from_rows(rows, config)


async def set_segment(state, company_id: str, key: str, config, note: str = "",
                      source: str = "you") -> Assignment:
    """Record which segment a company is in. Raises ValueError for a key
    you haven't configured."""
    key = (key or "").strip().lower()
    if key != UNKNOWN and by_key(config, key) is None:
        have = ", ".join(s.key for s in configured(config)) or "none configured"
        raise ValueError(f"No segment '{key}' in icp.segments ({have}).")
    from sellbird.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    note = note.strip()
    if not note:
        # Confirming a proposal keeps its reason.
        current = await company_segment(state, company_id, config)
        if current is not None and current.key == key:
            note = current.reason
    await state.add_observation(SEGMENT, company_id=company_id, collector=source,
                                value_text=json.dumps({"segment": key, "reason": note}),
                                confidence=TRUST.get(source, 0.7))
    return Assignment(key, source, note)


def size_conflict(segment, size: Size | None) -> str:
    """Why this headcount is certainly outside the segment's bounds, or ""."""
    if segment is None or size is None:
        return ""
    if segment.max_employees is not None and size.low > segment.max_employees:
        return f"{size.label()} employees, above {segment.name}'s {segment.max_employees:,}"
    if segment.min_employees is not None and size.high is not None and size.high < segment.min_employees:
        return f"{size.label()} employees, below {segment.name}'s {segment.min_employees:,}"
    return ""


def size_doubt(segment, size: Size | None) -> str:
    """Why this headcount might be outside the bounds (a range that straddles
    one), or "". Shown in the preview; it doesn't hold anyone."""
    if segment is None or size is None or size_conflict(segment, size):
        return ""
    top = size.high
    if segment.max_employees is not None and (top is None or top > segment.max_employees):
        return f"{size.label()} employees may be above {segment.name}'s {segment.max_employees:,}"
    if segment.min_employees is not None and size.low < segment.min_employees:
        return f"{size.label()} employees may be below {segment.name}'s {segment.min_employees:,}"
    return ""


SEGMENT_NEVER = "segment: not a fit"
SEGMENT_LATER = "segment parked"
SIZE_OUTSIDE = "outside its segment's size"


async def exclusion(state, company_id: str, config) -> str:
    """Why a company's segment keeps it from cold email now, or ""."""
    assignment = await company_segment(state, company_id, config)
    if assignment is None or assignment.segment(config) is None:
        return ""
    kind, detail = hold(assignment, await company_size(state, company_id), config)
    label = {"never": SEGMENT_NEVER, "later": SEGMENT_LATER, "size": SIZE_OUTSIDE}.get(kind)
    return f"{label} ({detail})" if label else ""


# ── What the Writer and handler are told ──

async def prompt_facts(state, company_id: str, config) -> tuple[list[str], object]:
    """(fact lines, segment or None) for a prospect's company."""
    lines, seg = [], None
    assignment = await company_segment(state, company_id, config)
    if assignment is not None:
        seg = assignment.segment(config)
        if seg is not None:
            lines.append(f"- Kind of company (your segment): {seg.name}")
    size = await company_size(state, company_id)
    if size is not None:
        lines.append(f"- Employees: {size.label()}")
    return lines, seg


def playbook_block(brain, segment) -> str:
    """The segment's playbook as a prompt section ("" when it has none)."""
    if segment is None or not segment.playbook or not hasattr(brain, "load_skill"):
        return ""
    text = (brain.load_skill(segment.playbook) or "").strip()
    if not text:
        return ""
    return (f"\n\nSEGMENT PLAYBOOK: {segment.name.upper()}\n"
            "This company is in your segment above. Its playbook decides the offer, the angle "
            "and the call to action; where anything earlier in these instructions suggests a "
            "different offer, follow the playbook.\n" + text)


# ── Proposing segments ──

def _card(index: int, company, size: Size | None, said: str, titles: list[str]) -> str:
    lines = [f"[c{index}] {company.name or company.domain}",
             f"  website: {company.domain or company.website or 'unknown'}"]
    if company.industry:
        lines.append(f"  labelled by search as: {company.industry}")
    if size is not None:
        lines.append(f"  employees: {size.label()}")
    if company.location:
        lines.append(f"  location: {company.location}")
    if said:
        lines.append(f"  its site mentions: {said}")
    if titles:
        lines.append(f"  contacts' titles: {', '.join(titles[:4])}")
    desc = " ".join((company.description or "").split())[:DESCRIPTION_MAX]
    if desc:
        lines.append(f"  what it says about itself: {desc}")
    return "\n".join(lines)


def classify_prompt(config, cards: list[str]) -> str:
    product = getattr(config, "product", None)
    segs = []
    for s in configured(config):
        bounds = []
        if s.min_employees is not None:
            bounds.append(f"at least {s.min_employees:,} employees")
        if s.max_employees is not None:
            bounds.append(f"at most {s.max_employees:,} employees")
        segs.append(f"- {s.key}: {s.name}. {s.description}"
                    + (f" ({'; '.join(bounds)})" if bounds else ""))
    return f"""You sort companies into the segments a seller uses to decide how to approach them.

The seller sells: {getattr(product, 'name', '')} - {getattr(product, 'description', '')}

SEGMENTS
{chr(10).join(segs)}
- {UNKNOWN}: there isn't enough to tell.

For each company, pick the one segment it belongs to by what the company IS (its
business model and scale), not by the label search gave it: a consultancy labelled as
a Splunk user is still a consultancy, and a recruiter or software vendor labelled as a
managed service provider is neither. Use what you reliably know about well-known
companies (their size, ownership, what they sell). Size bounds are a guide when the
headcount is known. Say {UNKNOWN} rather than guess.

COMPANIES
{fence(chr(10).join(cards), "company research")}

{UNTRUSTED_RULE}

Return JSON: {{"companies": [{{"id": "c1", "segment": "<key>", "reason": "<one short sentence: what the company is>"}}]}}"""


async def classify(state, brain, config, companies: list, force: bool = False) -> dict:
    """Propose a segment for each company that has none (all of them with
    ``force``, though never over one you set). Returns counts."""
    from sellbird.fit import site_said
    from sellbird.signals import seed_signal_catalog

    done = {"proposed": 0, "unknown": 0, "kept": 0, "failed": 0}
    if not configured(config) or brain is None or not companies:
        return done
    await seed_signal_catalog(state)
    todo = []
    for company in companies:
        current = await company_segment(state, company.id, config)
        if current is not None and (current.confirmed or not force):
            done["kept"] += 1
            continue
        todo.append(company)
    keys = {s.key for s in configured(config)} | {UNKNOWN}
    for start in range(0, len(todo), CLASSIFY_BATCH):
        chunk = todo[start:start + CLASSIFY_BATCH]
        cards = []
        for i, company in enumerate(chunk, 1):
            size = await company_size(state, company.id, company)
            try:
                obs = await state.get_observations(company_id=company.id, signal_code="SITE_MENTIONS")
                people = await state.get_prospects_for_company(company.id)
            except Exception:
                obs, people = [], []
            titles = [p.title for p in people if getattr(p, "title", "")]
            cards.append(_card(i, company, size, site_said(obs), titles))
        result = await brain.think_json(classify_prompt(config, cards), session_id="sellbird-segments",
                                        agent="scout", task="classify_segments")
        rows = result.get("companies") if isinstance(result, dict) else result
        if not isinstance(rows, list):
            done["failed"] += len(chunk)
            if getattr(brain, "paused_reason", ""):
                break
            continue
        answered = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            m = re.fullmatch(r"c(\d+)", str(row.get("id") or "").strip())
            key = str(row.get("segment") or "").strip().lower()
            if not m or key not in keys:
                continue
            idx = int(m.group(1)) - 1
            if not 0 <= idx < len(chunk) or idx in answered:
                continue
            answered.add(idx)
            reason = " ".join(str(row.get("reason") or "").split())[:200]
            await state.add_observation(
                SEGMENT, company_id=chunk[idx].id, collector=CLASSIFIER,
                value_text=json.dumps({"segment": key, "reason": reason}),
                confidence=TRUST[CLASSIFIER])
            done["unknown" if key == UNKNOWN else "proposed"] += 1
        done["failed"] += len(chunk) - len(answered)
    return done
