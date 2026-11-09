"""Geography: which ``icp.geography`` entry a company is in.

``icp.geography`` is the territory, in priority order: the first entry is
where the user most wants to sell, the next is the next best. This is the
tiered-account model account-based sales uses (Tier 1, 2, 3), and the
scarce thing (drafts, sends) goes to the top tier first.

Together the entries are also the boundary. A company Sellbird can place
in a country, and that is in no entry, is out of territory: it isn't
written to and nothing queued for it is sent (``icp.outside_geography:
skip``, the default). ``last`` keeps it, ranked after every entry. A
company whose location says nothing is never out of territory, only last,
and when any entry is a name Sellbird can't place ("Western Europe" with no
``icp.regions`` definition) nobody is ruled out, since it can't tell who is
in that entry.

An entry matches a company when:

- it has ``icp.geo_coordinates`` with a radius ("lat,lng,radius_km") and
  the company's cached coordinates are inside that circle;
- its place name is one of the company location's parts ("Seattle, WA"
  matches "Seattle, WA, USA" and "Seattle"), unless both name a country and
  the countries differ;
- it names a country alone ("United States", "Canada", "GB") and the
  company's location puts it in that country (see ``send_window.country_for``).

- it names a region, built in (REGIONS) or defined in ``icp.regions``,
  and the company's country or state/province is one of its members.
  Members are ISO codes: a country ("US", "MX") or a state or province
  ("US-WA", "CA-BC").

Anything else matches only by name. A company whose location says nothing
matches no entry, so it ranks with the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from sellbird.locations import split_sites
from sellbird.send_window import (
    _clean, _country_in, _km, _parts, _point, country_for, subdivision_for,
)

# Why a queued email to an out-of-territory company was cancelled.
OUTSIDE_GEOGRAPHY = "outside your geography"

# UN M49 sub-regions of the Americas (unstats.un.org/unsd/methodology/m49).
_NORTHERN_AMERICA = ("BM", "CA", "GL", "PM", "US")
_CENTRAL_AMERICA = ("BZ", "CR", "GT", "HN", "MX", "NI", "PA", "SV")
_SOUTH_AMERICA = ("AR", "BO", "BR", "BV", "CL", "CO", "EC", "FK", "GF", "GS", "GY",
                  "PE", "PY", "SR", "UY", "VE")
_CARIBBEAN = ("AG", "AI", "AW", "BB", "BL", "BQ", "BS", "CU", "CW", "DM", "DO", "GD",
              "GP", "HT", "JM", "KN", "KY", "LC", "MF", "MQ", "MS", "PR", "SX", "TC",
              "TT", "VC", "VG", "VI")
_LATIN_AMERICA = _CENTRAL_AMERICA + _SOUTH_AMERICA + _CARIBBEAN
_PACIFIC_NORTHWEST = ("US-WA", "US-OR", "US-ID", "CA-BC")

# Region names an icp.geography entry can use, by cleaned name. Countries
# follow UN M49, except "North America", which is the business sense (US,
# Canada, Mexico). The Pacific Northwest is Washington, Oregon, Idaho and
# British Columbia. icp.regions adds to these or redefines one.
REGIONS: dict[str, tuple[str, ...]] = {
    "americas": _NORTHERN_AMERICA + _LATIN_AMERICA,
    "the americas": _NORTHERN_AMERICA + _LATIN_AMERICA,
    "north america": ("US", "CA", "MX"),
    "northern america": _NORTHERN_AMERICA,
    "latin america": _LATIN_AMERICA,
    "latam": _LATIN_AMERICA,
    "central america": _CENTRAL_AMERICA,
    "south america": _SOUTH_AMERICA,
    "caribbean": _CARIBBEAN,
    "pacific northwest": _PACIFIC_NORTHWEST,
    "pnw": _PACIFIC_NORTHWEST,
}


def _radius(spec: str) -> tuple[tuple[float, float], float] | None:
    """(centre, radius_km) from a "lat,lng,radius_km" setting, or None."""
    centre = _point(spec)
    try:
        radius = float(str(spec or "").split(",")[2])
    except (IndexError, ValueError):
        return None
    return (centre, radius) if centre and radius > 0 else None


def _entry_country(entry: str) -> str:
    """The country an entry names when it is only a country, else ""."""
    parts = _parts(entry)
    return _country_in(parts) if len(parts) == 1 else ""


def region_members(entry: str, regions: dict | None = None) -> tuple[str, ...]:
    """The ISO codes a region entry stands for, or () when it isn't one.
    ``regions`` is icp.regions, which wins over the built-in REGIONS."""
    name = _clean(entry)
    for key, members in (regions or {}).items():
        if _clean(key) == name:
            return tuple(str(m).strip().upper() for m in members or () if str(m).strip())
    return REGIONS.get(name, ())


def _in_region(members: tuple[str, ...], location: str, point: str) -> bool:
    country = country_for(location, point)[0]
    if not country:
        return False
    if country in members:
        return True
    subdivision = subdivision_for(location, point)
    return bool(subdivision) and subdivision in members


def placeable(entry: str, coords: dict | None = None, regions: dict | None = None) -> bool:
    """True when Sellbird can tell whether a company is in ``entry``: a
    region, a country, a place in a country it can name, or a radius."""
    return bool(region_members(entry, regions) or _entry_country(entry)
                or _radius((coords or {}).get(entry, "")) or country_for(entry, "")[0])


def matches(entry: str, location: str, point: str = "", coords: dict | None = None,
            regions: dict | None = None) -> bool:
    """True when a company at ``location`` (cached ``point``) is in ``entry``."""
    loc_parts = [_clean(p) for p in _parts(location)]
    if not loc_parts and not point:
        return False
    circle = _radius((coords or {}).get(entry, ""))
    here = _point(point)
    if circle and here and _km(here, circle[0]) <= circle[1]:
        return True
    members = region_members(entry, regions)
    if members:
        return _in_region(members, location, point)
    country = _entry_country(entry)
    if country:
        return country_for(location, point)[0] == country
    entry_parts = _parts(entry)
    if not entry_parts:
        return False
    # _parts is last-first, so the place name is the last element.
    if _clean(entry_parts[-1]) not in loc_parts:
        return False
    theirs, ours = country_for(location, point)[0], country_for(entry, "")[0]
    return not (theirs and ours and theirs != ours)


def tier(location: str, config, point: str = "") -> tuple[int | None, str]:
    """(index into icp.geography, entry) for the first entry the company is
    in, or (None, "") when it is in none or nothing is configured."""
    icp = getattr(config, "icp", None)
    entries = list(getattr(icp, "geography", None) or [])
    coords = getattr(icp, "geo_coordinates", None) or {}
    regions = getattr(icp, "regions", None) or {}
    for i, entry in enumerate(entries):
        if matches(entry, location, point, coords, regions):
            return i, entry
    return None, ""


def outside(location: str, config, point: str = "") -> bool:
    """True when the company is out of territory and must not be emailed:
    ``icp.outside_geography`` is "skip", Sellbird can place the company in
    a country, every entry is one it can judge (``placeable``), and the
    company is in none of them."""
    icp = getattr(config, "icp", None)
    entries = list(getattr(icp, "geography", None) or [])
    if not entries or (getattr(icp, "outside_geography", "skip") or "skip") != "skip":
        return False
    if not country_for(location, point)[0]:
        return False
    coords = getattr(icp, "geo_coordinates", None) or {}
    regions = getattr(icp, "regions", None) or {}
    if not all(placeable(e, coords, regions) for e in entries):
        return False
    return tier(location, config, point)[0] is None


def rank(location: str, config, point: str = "") -> int:
    """A sort key: the tier index, or one past the last entry for a company
    in none, so "no match" sorts after every match and never drops out."""
    index, _ = tier(location, config, point)
    if index is not None:
        return index
    return len(getattr(getattr(config, "icp", None), "geography", None) or [])


# ── Placing a person ──
#
# Sellbird sells in person first, so the question is where this person can be
# met. Evidence is weighed by precedence, not combined (the survivorship rule
# master-data systems use): the person's own location (remote included) wins
# outright, and only when it's unknown does the company's footprint stand in,
# as a weaker guess the more sites it has.

PERSON, COMPANY, PRESENCE, UNKNOWN = "person", "company", "presence", "unknown"
# Within a tier: known to be there, then a one-site company there, then a
# company with one of several sites there.
CERTAINTY = {PERSON: 0, COMPANY: 1, PRESENCE: 2, UNKNOWN: 3}


@dataclass(frozen=True)
class Placement:
    tier: int | None      # index into icp.geography, None when in no entry
    entry: str            # that entry, or ""
    basis: str            # PERSON, COMPANY, PRESENCE or UNKNOWN
    outside: bool         # out of territory: never emailed
    where: str            # the location the answer rests on, for reasons

    def key(self, entries: int) -> tuple[int, int]:
        """Sort key: tier (no tier after every entry), then certainty."""
        return (self.tier if self.tier is not None else entries, CERTAINTY[self.basis])

    def reason(self) -> str:
        if self.outside:
            return f"outside your geography ({self.where})"
        if self.tier is None:
            return "location unknown" if self.basis == UNKNOWN else f"in none of your regions ({self.where})"
        how = {PERSON: "works in", COMPANY: "company is in",
               PRESENCE: "company has a site in"}[self.basis]
        return f"{self.entry} (priority {self.tier + 1}): {how} {self.where}"


def place(config, person: tuple[str, str] | None = None,
          sites: list[tuple[str, str]] | None = None) -> Placement:
    """Where a person stands in icp.geography.

    ``person`` is their own (location, point), ``sites`` the company's
    (location, point) list. A person location counts only when Sellbird can
    place it in a country ("Remote" can't be, so the company stands in)."""
    if person and person[0].strip() and country_for(person[0], person[1])[0]:
        loc, point = person
        index, entry = tier(loc, config, point)
        return Placement(index, entry, PERSON, outside(loc, config, point), loc.strip())
    known = [(loc.strip(), point) for loc, point in (sites or []) if loc and loc.strip()]
    known = list(dict(known).items())  # one per distinct location
    if not known:
        return Placement(None, "", UNKNOWN, False, "")
    basis = COMPANY if len(known) == 1 else PRESENCE
    hits = [(tier(loc, config, point), loc) for loc, point in known]
    best = min(((t, loc) for t, loc in hits if t[0] is not None),
               key=lambda h: h[0][0], default=None)
    if best is not None:
        (index, entry), loc = best
        return Placement(index, entry, basis, False, loc)
    gone = all(outside(loc, config, point) for loc, point in known)
    return Placement(None, "", basis, gone, "; ".join(loc for loc, _ in known)[:200])


def company_place(company: dict, observations: list[dict], config) -> Placement:
    """``place`` for a company with no particular person in mind, from its
    location field and the newest COMPANY_SITE among ``observations``."""
    sites = [company.get("location") or ""]
    rows = sorted((o for o in observations or [] if o.get("signal_code") == "COMPANY_SITE"),
                  key=lambda o: str(o.get("observed_at") or ""), reverse=True)
    if rows:
        sites += split_sites(rows[0].get("value_text") or "")
    return place(config, None, [(s, "") for s in sites])


async def _point_for(state, location: str) -> str:
    cached = await state.get_setting("geo:" + location.strip().lower()) if location.strip() else ""
    return "" if not cached or cached == "-" else cached


async def company_sites(state, company_id: str, location: str = "") -> list[tuple[str, str]]:
    """The company's (location, point) list: its own location field, then
    the offices its site lists (the newest COMPANY_SITE observation)."""
    places = [location] if location and location.strip() else []
    if company_id:
        rows = await state.get_observations(company_id=company_id, signal_code="COMPANY_SITE",
                                            latest_only=True, limit=1)
        if rows:
            places += split_sites(rows[0].get("value_text") or "")
    return [(p, await _point_for(state, p)) for p in places]


async def person_location(state, prospect_id: str) -> str:
    """The person's location: the most confident PERSON_LOCATION, newest
    first among equals, or ""."""
    rows = await state.get_prospect_observations(prospect_id, "PERSON_LOCATION")
    rows = [r for r in rows if (r.get("value_text") or "").strip()]
    if not rows:
        return ""
    best = max(rows, key=lambda r: (float(r.get("confidence") or 0), str(r.get("observed_at") or "")))
    return best["value_text"].strip()


async def prospect_place(state, prospect, config) -> Placement:
    """``place`` for a prospect, from what Sellbird knows about them and
    their company."""
    person = await person_location(state, prospect.id) if getattr(prospect, "id", "") else ""
    company_id = getattr(prospect, "company_id", "") or ""
    location = ""
    if company_id:
        company = await state.get_company(company_id)
        location = (getattr(company, "location", "") or "") if company else ""
    sites = await company_sites(state, company_id, location)
    return place(config, (person, await _point_for(state, person)) if person else None, sites)


async def prospect_rank(state, prospect, config) -> tuple[int, int]:
    """A prospect's sort key: tier, then how sure Sellbird is of it."""
    entries = len(getattr(getattr(config, "icp", None), "geography", None) or [])
    return (await prospect_place(state, prospect, config)).key(entries)


async def prospect_outside(state, prospect, config) -> str:
    """Where the prospect is when they're out of territory (the reason to
    show), else ""."""
    p = await prospect_place(state, prospect, config)
    return p.where if p.outside else ""
