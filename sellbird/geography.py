"""Geography: which ``icp.geography`` entry a person or company is in.

``icp.geography`` is the territory, in priority order: the first entry is
where the user most wants to sell, the next is the next best. This is the
tiered-account model account-based sales uses (Tier 1, 2, 3), and the
scarce thing (drafts, sends) goes to the top tier first.

Everything here works on structured places (``sellbird.locations.Place``:
city, ISO 3166-2 region, ISO 3166-1 country), never on re-parsed text.
Free text is turned into a place once, by the reference tables or the
geocoder, and what neither can settle is unknown, not guessed.

An entry matches a place when:

- it has ``icp.geo_coordinates`` with a radius ("lat,lng,radius_km") and
  the place's coordinates are inside that circle;
- it names a region, built in (REGIONS) or defined in ``icp.regions``, and
  the place's country or ISO 3166-2 region is one of its members ("US",
  "US-WA");
- it is a country ("United States") and the place is in it;
- it is a state or province ("Washington, US") and the place is in it;
- it is a city ("Seattle, WA") and the place is that city, in the same
  region and country.

Together the entries are also the boundary: a place in a known country that
matches no entry is out of territory (``icp.outside_geography: skip``, the
default; ``last`` keeps it, ranked after every entry). An unknown place is
never out, only last, and when any entry is one Sellbird can't judge
("Western Europe" with no ``icp.regions`` definition) nobody is ruled out.
"""

from __future__ import annotations

from dataclasses import dataclass

from sellbird.locations import (
    Place, cached_from, cached_place, parse_places, place_by_convention, place_from_text,
)
from sellbird.send_window import _clean, _km, _point

# Why a queued email to an out-of-territory person was cancelled.
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


def region_members(entry: str, regions: dict | None = None) -> tuple[str, ...]:
    """The ISO codes a region entry stands for, or () when it isn't one.
    ``regions`` is icp.regions, which wins over the built-in REGIONS."""
    name = _clean(entry)
    for key, members in (regions or {}).items():
        if _clean(key) == name:
            return tuple(str(m).strip().upper() for m in members or () if str(m).strip())
    return REGIONS.get(name, ())


def entry_place(entry: str) -> Place | None:
    """The place an entry names ("Seattle, WA", "United States", "Denver,
    CO": you wrote it about your own territory, so a code after a city is
    the state), or None for a name Sellbird can't place."""
    return place_by_convention(entry)


def placeable(entry: str, coords: dict | None = None, regions: dict | None = None) -> bool:
    """True when Sellbird can tell whether a place is in ``entry``."""
    return bool(region_members(entry, regions) or _radius((coords or {}).get(entry, ""))
                or entry_place(entry))


def _cities(city: str) -> set[str]:
    """"Tukwila/Seattle" -> {"tukwila", "seattle"}."""
    return {_clean(c) for c in city.replace("/", ",").split(",") if c.strip()}


def matches_place(entry: str, place: Place | None, coords: dict | None = None,
                  regions: dict | None = None) -> bool:
    """True when ``place`` is in ``entry``."""
    if place is None or not place.known:
        return False
    circle = _radius((coords or {}).get(entry, ""))
    here = _point(place.point())
    if circle and here and _km(here, circle[0]) <= circle[1]:
        return True
    members = region_members(entry, regions)
    if members:
        return place.country in members or (bool(place.region) and place.region in members)
    target = entry_place(entry)
    if target is None or target.country != place.country:
        return False
    if target.city:
        return ((not target.region or target.region == place.region)
                and bool(_cities(target.city) & _cities(place.city)))
    if target.region:
        return target.region == place.region
    return True


def tier_place(place: Place | None, config) -> tuple[int | None, str]:
    """(index into icp.geography, entry) for the first entry the place is
    in, or (None, "") when it is in none or nothing is configured."""
    icp = getattr(config, "icp", None)
    coords = getattr(icp, "geo_coordinates", None) or {}
    regions = getattr(icp, "regions", None) or {}
    for i, entry in enumerate(getattr(icp, "geography", None) or []):
        if matches_place(entry, place, coords, regions):
            return i, entry
    return None, ""


def outside_place(place: Place | None, config) -> bool:
    """True when the place is out of territory: ``icp.outside_geography`` is
    "skip", the place is in a known country, every entry is one Sellbird can
    judge, and the place is in none of them."""
    icp = getattr(config, "icp", None)
    entries = list(getattr(icp, "geography", None) or [])
    if not entries or (getattr(icp, "outside_geography", "skip") or "skip") != "skip":
        return False
    if place is None or not place.known:
        return False
    coords = getattr(icp, "geo_coordinates", None) or {}
    regions = getattr(icp, "regions", None) or {}
    if not all(placeable(e, coords, regions) for e in entries):
        return False
    return tier_place(place, config)[0] is None


# Text forms, for free text the reference tables settle on their own
# (ambiguous text is unknown here: resolve it first, see locations).

def matches(entry: str, location: str, point: str = "", coords: dict | None = None,
            regions: dict | None = None) -> bool:
    return matches_place(entry, place_from_text(location, point)[0], coords, regions)


def tier(location: str, config, point: str = "") -> tuple[int | None, str]:
    return tier_place(place_from_text(location, point)[0], config)


def outside(location: str, config, point: str = "") -> bool:
    return outside_place(place_from_text(location, point)[0], config)


def rank(location: str, config, point: str = "") -> int:
    """A sort key: the tier index, or one past the last entry for a place in
    none, so "no match" sorts after every match and never drops out."""
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
    tier: int | None        # index into icp.geography, None when in no entry
    entry: str              # that entry, or ""
    basis: str              # PERSON, COMPANY, PRESENCE or UNKNOWN
    outside: bool           # out of territory: never emailed
    where: str              # the place the answer rests on, for reasons
    unresolved: tuple = ()  # free text that couldn't be placed: you settle it

    def key(self, entries: int) -> tuple[int, int]:
        """Sort key: tier (no tier after every entry), then certainty."""
        return (self.tier if self.tier is not None else entries, CERTAINTY[self.basis])

    def reason(self) -> str:
        if self.outside:
            return f"outside your geography ({self.where})"
        if self.tier is None:
            if self.basis == UNKNOWN:
                return (f"location unknown: \"{'; '.join(self.unresolved)}\" needs you"
                        if self.unresolved else "location unknown")
            return f"in none of your regions ({self.where})"
        how = {PERSON: "works in", COMPANY: "company is in",
               PRESENCE: "company has a site in"}[self.basis]
        return f"{self.entry} (priority {self.tier + 1}): {how} {self.where}"


def _key(p: Place) -> tuple:
    return (p.city.lower(), p.region, p.country)


def _inside(specific: Place, broad: Place) -> bool:
    """``specific`` names a place within ``broad`` and in more detail
    (Washington, DC, US within US; Seattle, WA within WA)."""
    if specific.country != broad.country:
        return False
    if broad.city:
        return False                      # a city has nothing finer here
    if broad.region:
        return specific.region == broad.region and bool(specific.city)
    return bool(specific.region or specific.city)


def place(config, person: Place | None = None, sites: list[Place] | None = None,
          unresolved: tuple = ()) -> Placement:
    """Where a person stands in icp.geography: their own place when known,
    else the company's sites. ``unresolved`` is free text that couldn't be
    placed, carried so the answer can say what needs you."""
    if person is not None and person.known and not (person.city or person.region):
        # Only their country (a free-plan provider): it beats the company
        # when they disagree (a remote engineer abroad), but when the
        # company has a site in that country, that site is the same answer
        # with more detail, so it decides (survivorship by specificity).
        same = [p for p in sites or [] if p is not None and p.known and p.country == person.country]
        if same:
            return place(config, None, same, unresolved)
    if person is not None and person.known:
        index, entry = tier_place(person, config)
        return Placement(index, entry, PERSON, outside_place(person, config), person.label())
    known: list[Place] = []
    for p in sites or []:
        if p is not None and p.known and _key(p) not in {_key(k) for k in known}:
            known.append(p)
    # "US" and "Washington, DC, US" from two sources are one site at two
    # levels of detail, not two sites: a site another, more specific one
    # falls inside is dropped.
    known = [p for p in known if not any(_inside(o, p) for o in known if o is not p)]
    if not known:
        return Placement(None, "", UNKNOWN, False, "", tuple(unresolved))
    basis = COMPANY if len(known) == 1 and not unresolved else PRESENCE
    hits = [(tier_place(p, config), p) for p in known]
    best = min(((t, p) for t, p in hits if t[0] is not None), key=lambda h: h[0][0], default=None)
    if best is not None:
        (index, entry), p = best
        return Placement(index, entry, basis, False, p.label(), tuple(unresolved))
    # Out only when every site is known and out: an unplaced one might be in.
    gone = not unresolved and all(outside_place(p, config) for p in known)
    return Placement(None, "", basis, gone, "; ".join(p.label() for p in known)[:200],
                     tuple(unresolved))


def _latest_site_values(observations: list[dict]) -> list:
    """Every source's newest COMPANY_SITE (the profile's, your CRM's, yours),
    so one source's re-read never erases another's."""
    newest: dict[str, dict] = {}
    for o in observations or []:
        if o.get("signal_code") != "COMPANY_SITE":
            continue
        src = o.get("collector") or ""
        if src not in newest or str(o.get("observed_at") or "") > str(newest[src].get("observed_at") or ""):
            newest[src] = o
    values: list = []
    for o in newest.values():
        values += parse_places(o.get("value_text") or "")
    return values


def _split(values: list, resolve) -> tuple[list[Place], list[str]]:
    places, unresolved = [], []
    for v in values:
        if isinstance(v, Place):
            places.append(v)
            continue
        text = str(v or "").strip()
        if not text:
            continue
        p, ambiguous = resolve(text)
        if p:
            places.append(p)
        elif ambiguous:
            unresolved.append(text)
    return places, unresolved


def company_place(company: dict, observations: list[dict], config,
                  cache: dict | None = None) -> Placement:
    """``place`` for a company with no particular person in mind: its
    location field (read by its source's declared format) and every
    source's newest COMPANY_SITE. ``cache`` holds the place: and
    location-format: settings, so nothing is looked up here."""
    values = _site_values(company.get("location") or "", observations)
    source = company.get("source") or ""
    sites, unresolved = _split(values, lambda t: cached_from(cache or {}, t, "", source))
    return place(config, None, sites, tuple(unresolved))


async def _split_async(state, values: list, source: str = "") -> tuple[list[Place], list[str]]:
    places, unresolved = [], []
    for v in values:
        if isinstance(v, Place):
            places.append(v)
            continue
        text = str(v or "").strip()
        if not text:
            continue
        p, ambiguous = await cached_place(state, text)
        if p:
            places.append(p)
        elif ambiguous:
            unresolved.append(text)
    return places, unresolved


# Sources that state where the company is: they supersede the free-text
# location field, which holds the same fact from a lower-trust source.
AUTHORITATIVE = ("you", "crm")


def _site_values(location: str, observations: list[dict]) -> list:
    rows = [o for o in observations or [] if o.get("signal_code") == "COMPANY_SITE"]
    stated = any((o.get("collector") or "") in AUTHORITATIVE for o in rows)
    return ([] if stated else [location]) + _latest_site_values(rows)


async def company_sites(state, company_id: str, location: str = "",
                        source: str = "") -> tuple[list[Place], list[str]]:
    """The company's places, and the free text that couldn't be placed.
    ``source`` is where the company came from, whose declared format reads
    its location field."""
    rows = []
    if company_id:
        rows = await state.get_observations(company_id=company_id, signal_code="COMPANY_SITE",
                                            limit=50)
    return await _split_async(state, _site_values(location, rows), source)


def _specificity(p: Place) -> int:
    return (1 if p.country else 0) + (1 if p.region else 0) + (1 if p.city else 0)


def _agrees(general: Place, specific: Place) -> bool:
    """``specific`` refines ``general``: same country, same region if it has one."""
    return (general.country == specific.country
            and (not general.region or general.region == specific.region)
            and (not general.city or _cities(general.city) & _cities(specific.city)))


async def person_location(state, prospect_id: str) -> Place | str | None:
    """The person's location: the most trusted PERSON_LOCATION (newest
    among equals), refined by a less trusted one that agrees with it and
    says more: a provider's country plus another's city in that country is
    the city. Older free text comes back as a string."""
    rows = await state.get_prospect_observations(prospect_id, "PERSON_LOCATION")
    rows = [r for r in rows if (r.get("value_text") or "").strip()]
    if not rows:
        return None
    rows.sort(key=lambda r: (float(r.get("confidence") or 0), str(r.get("observed_at") or "")),
              reverse=True)
    parsed = parse_places(rows[0]["value_text"])
    best = parsed[0] if parsed else None
    if not isinstance(best, Place):
        return best
    for r in rows[1:]:
        other = next(iter(parse_places(r["value_text"])), None)
        if isinstance(other, Place) and _specificity(other) > _specificity(best) and _agrees(best, other):
            best = other
    return best


async def prospect_place(state, prospect, config) -> Placement:
    """``place`` for a prospect, from what Sellbird knows about them and
    their company."""
    person = await person_location(state, prospect.id) if getattr(prospect, "id", "") else None
    unresolved: list[str] = []
    if isinstance(person, str):
        found, ambiguous = await cached_place(state, person)
        if ambiguous:
            unresolved.append(person)
        person = found
    company_id = getattr(prospect, "company_id", "") or ""
    location = source = ""
    if company_id:
        company = await state.get_company(company_id)
        location = (getattr(company, "location", "") or "") if company else ""
        source = (getattr(company, "source", "") or "") if company else ""
    sites, site_unresolved = await company_sites(state, company_id, location, source)
    return place(config, person, sites, tuple(unresolved + site_unresolved))


async def prospect_rank(state, prospect, config) -> tuple[int, int]:
    """A prospect's sort key: tier, then how sure Sellbird is of it."""
    entries = len(getattr(getattr(config, "icp", None), "geography", None) or [])
    return (await prospect_place(state, prospect, config)).key(entries)


async def prospect_outside(state, prospect, config) -> str:
    """Where the prospect is when they're out of territory (the reason to
    show), else ""."""
    p = await prospect_place(state, prospect, config)
    return p.where if p.outside else ""
