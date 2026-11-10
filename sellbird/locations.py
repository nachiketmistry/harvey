"""Where a company has sites, read from its own pages.

A company's offices come from the schema.org address data it publishes
(JSON-LD ``PostalAddress``): the Organization or LocalBusiness on the
homepage, the branches on a contact or locations page. That is the company
describing itself in a structured way, so it needs no guessing at free
text. Addresses under a Person, an Event, a JobPosting or a Review are
someone else's place, a venue, or a role's location, and are skipped.

Sites are stored as one ``COMPANY_SITE`` observation per profile run:
``value_text`` is the sites joined by " | ", ``value_num`` how many. The
newest observation is the current footprint, so an office the site stops
listing drops out.
"""

from __future__ import annotations

import json
import re


def _json_blocks(html: str) -> list:
    out = []
    for raw in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or ""):
        try:
            out.append(json.loads(raw.strip()))
        except (ValueError, TypeError, RecursionError):
            continue
    return out


SITE_SEPARATOR = " | "
# More than this and the page is a store locator: the company is everywhere,
# which says nothing about where a given person works.
MAX_SITES = 25
_SKIP_TYPES = {"person", "event", "jobposting", "review", "offer", "aggregaterating",
               "breadcrumblist", "imageobject", "videoobject"}
LOCATION_PATHS = ("/locations", "/offices", "/our-offices", "/contact", "/contact-us",
                  "/about/locations", "/company/locations")


def _types(node: dict) -> set[str]:
    t = node.get("@type", "")
    return {x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}


def _text(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@id") or ""
    return " ".join(str(value or "").split())


def _walk(data, out: list) -> None:
    if isinstance(data, list):
        for item in data:
            _walk(item, out)
        return
    if not isinstance(data, dict):
        return
    types = _types(data)
    if types & _SKIP_TYPES:
        return
    if "postaladdress" in types:
        place = place_from_postal(data)
        if place and (place.city or place.region or place.country):
            out.append(place)
        return
    for key, value in data.items():
        if key == "address" and isinstance(value, str):
            continue  # a one-line street address can't be placed reliably
        _walk(value, out)


def sites_from_schema(html: str) -> list["Place"]:
    """The distinct places a page's JSON-LD addresses give, in page order,
    each field from its own address field (addressCountry is the country)."""
    found: list = []
    for block in _json_blocks(html):
        _walk(block, found)
    seen, out = set(), []
    for place in found:
        key = (place.city.lower(), place.region, place.country)
        if key not in seen:
            seen.add(key)
            out.append(place)
    return out


def join_sites(sites: list[str]) -> str:
    return SITE_SEPARATOR.join(sites)


def split_sites(text: str) -> list[str]:
    return [s.strip() for s in str(text or "").split(SITE_SEPARATOR.strip()) if s.strip()]


# ── Places: a location as structured data ─────────────────────────────
#
# A location is stored the way schema.org's PostalAddress and CRM address
# fields keep it: a city, an ISO 3166-2 region ("US-WA") and an ISO 3166-1
# country ("DE"), each filled from the field it came in, never parsed back
# out of one string. A country column that says "DE" is Germany by
# construction; only free text ("Pittsburgh, PA") needs resolving, and that
# goes through the reference tables, then the geocoder. What neither can
# settle (a bare "DE": Germany or Delaware?) stays unknown and is listed
# for you to resolve, never guessed.

from dataclasses import dataclass  # noqa: E402

UNRESOLVED = "?"   # cached: free text Sellbird couldn't settle; you resolve it
NO_PLACE = "-"     # cached: nothing to place
PLACE_CACHE = "place:"


@dataclass(frozen=True)
class Place:
    city: str = ""
    region: str = ""        # ISO 3166-2, e.g. "US-WA"; "" when unknown
    country: str = ""       # ISO 3166-1 alpha-2, e.g. "DE"; "" when unknown
    lat: float | None = None
    lng: float | None = None

    @property
    def known(self) -> bool:
        return bool(self.country)

    def point(self) -> str:
        return f"{self.lat:.4f},{self.lng:.4f}" if self.lat is not None and self.lng is not None else ""

    def label(self) -> str:
        region = self.region.split("-", 1)[1] if "-" in self.region else self.region
        return ", ".join(p for p in (self.city, region, self.country) if p)

    def to_json(self) -> str:
        data = {k: v for k, v in (("city", self.city), ("region", self.region),
                                  ("country", self.country), ("lat", self.lat),
                                  ("lng", self.lng)) if v not in ("", None)}
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text) -> "Place | None":
        try:
            data = json.loads(text) if isinstance(text, str) else text
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        def num(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return cls(city=str(data.get("city") or ""), region=str(data.get("region") or "").upper(),
                   country=str(data.get("country") or "").upper(),
                   lat=num(data.get("lat")), lng=num(data.get("lng")))


def _tables():
    from sellbird import send_window as sw
    return sw


def country_from_field(value) -> str:
    """A country field's value as ISO 3166-1 alpha-2: a code ("DE", "uk",
    "USA") or a name ("Germany", "United States"). In a country field a code
    is the country, so "DE" is Germany; "" when it names none."""
    sw = _tables()
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    code = sw.country_code(text)
    if code:
        return code
    if text.upper() in ("USA", "U.S.", "U.S.A."):
        return "US"
    return sw._country_names().get(sw._clean(text), "")


def region_from_field(value, country: str) -> str:
    """A region field's value as ISO 3166-2 ("WA" or "Washington" in the US
    is "US-WA"), for the US, Canada and Australia; "" otherwise."""
    sw = _tables()
    text = sw._clean(value)
    if not text or country not in ("US", "CA", "AU"):
        return ""
    table = {"US": sw.US_STATES, "CA": sw.CA_PROVINCES, "AU": sw.AU_STATES}[country]
    code = text.upper().replace(f"{country}-", "")
    if code in table:
        return f"{country}-{code}"
    for key, (name, _) in table.items():
        if text == name:
            return f"{country}-{key}"
    alias = sw.REGION_ALIASES.get(text, "") if country == "CA" else ""
    return f"CA-{alias}" if alias else ""


def place_from_fields(city="", region="", country="") -> Place | None:
    """A Place from separate city, region and country fields (a CRM export,
    a schema.org address). None when there's nothing to place."""
    code = country_from_field(country)
    region_code = region_from_field(region, code) if code else ""
    if not code and region:
        # A region with no country column settles the country only when it
        # names a region in one country alone ("Texas"; "WA" is Washington
        # or Western Australia, so it doesn't).
        hits = [(c, r) for c in ("US", "CA", "AU") for r in [region_from_field(region, c)] if r]
        code, region_code = hits[0] if len(hits) == 1 else ("", "")
    city = " ".join(str(city or "").split())
    if city.islower():
        # Some providers (People Data Labs) lowercase every field.
        city = city.title()
    if not (code or city):
        return None
    return Place(city=city, region=region_code, country=code)


def place_from_postal(address: dict) -> Place | None:
    """A Place from a schema.org PostalAddress, field by field."""
    def text(v):
        if isinstance(v, dict):
            v = v.get("name") or v.get("@id") or ""
        return " ".join(str(v or "").split())
    return place_from_fields(text(address.get("addressLocality")), text(address.get("addressRegion")),
                             text(address.get("addressCountry")))


def place_from_text(text: str, point: str = "") -> tuple[Place | None, bool]:
    """(Place, False) when the reference tables settle free text on their
    own, (None, True) when they can't ("DE": Germany or Delaware), (None,
    False) when it names nowhere. No network: see resolve_text."""
    sw = _tables()
    text = " ".join(str(text or "").split())
    if not text:
        return None, False
    country, certain = sw.country_for(text, point)
    if not country:
        return None, False
    if not certain:
        return None, True
    region = sw.subdivision_for(text, point)
    parts = sw._parts(text)
    city = parts[-1] if len(parts) > 1 else ""
    if city and (sw._clean(city) in sw._country_names() or region_from_field(city, country)):
        city = ""
    latlng = sw._point(point)
    return Place(city=city, region=region if region.startswith(country + "-") else "",
                 country=country, lat=latlng[0] if latlng else None,
                 lng=latlng[1] if latlng else None), False


def place_by_convention(text: str, point: str = "") -> Place | None:
    """Free text read the way send_window reads it when the tables can't
    settle it alone: a code after a city is a US state ("Denver, CO").

    Only for text the user wrote about their own territory (icp.geography
    entries), where that reading is what they mean. Never for prospect or
    company data, which goes through resolve_text."""
    place, ambiguous = place_from_text(text, point)
    if place or not ambiguous:
        return place
    sw = _tables()
    country = sw.country_for(text, point)[0]
    if not country:
        return None
    parts = sw._parts(text)
    code, _ = sw._region_hit(parts, country, "")
    city = parts[-1] if len(parts) > 1 else ""
    return Place(city=city, region=code if code.startswith(country + "-") else "", country=country)


def _cache_key(text: str) -> str:
    return PLACE_CACHE + " ".join(str(text or "").split()).lower()


# What a source writes in a company's location field, as you declared it
# (`sellbird locations --source drew-crm --codes-are countries`): a field
# mapping, the way CRM integrations declare what each column means.
SOURCE_FORMAT = "location-format:"
CODES_ARE_COUNTRIES = "countries"


def from_source(text: str, source_format: str) -> Place | None:
    """A location field read by its source's declared format: with
    "countries", a bare code is that country ("DE" is Germany)."""
    if source_format == CODES_ARE_COUNTRIES and _bare_code(text):
        code = country_from_field(text)
        return Place(country=code) if code else None
    return None


def cached_from(cache: dict, text: str, point: str = "", source: str = "") -> tuple[Place | None, bool]:
    """resolve_text's answer from a dict of cached settings, without the
    network: the source's declared format, the cache (filled by
    resolve_text, or by you), else the tables."""
    if source:
        declared = from_source(text, cache.get(SOURCE_FORMAT + source, ""))
        if declared:
            return declared, False
    hit = cache.get(_cache_key(text))
    if hit == UNRESOLVED:
        return None, True
    if hit == NO_PLACE:
        return None, False
    if hit:
        place = Place.from_json(hit)
        if place:
            return place, False
    return place_from_text(text, point)


async def cached_place(state, text: str, point: str = "", source: str = "") -> tuple[Place | None, bool]:
    """cached_from, reading only the settings this text needs."""
    key = _cache_key(text)
    cache = {key: await state.get_setting(key)}
    if source:
        cache[SOURCE_FORMAT + source] = await state.get_setting(SOURCE_FORMAT + source)
    return cached_from(cache, text, point, source)


async def geocode_place(text: str, client=None) -> Place | None:
    """Free text through Nominatim, with the structured address it returns:
    ISO 3166-2 region and country code. None on no match or an error."""
    import httpx
    from sellbird.collectors.discover import GEOCODE_UA, NOMINATIM
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        r = await client.get(NOMINATIM, params={"q": text, "format": "json", "limit": 1,
                                                "addressdetails": 1},
                             headers={"User-Agent": GEOCODE_UA})
        r.raise_for_status()
        hits = r.json()
    except Exception:
        return None
    finally:
        if owned:
            await client.aclose()
    if not hits or not isinstance(hits, list):
        return None
    hit = hits[0]
    addr = hit.get("address") or {}
    country = str(addr.get("country_code") or "").upper()
    if not country:
        return None
    region = str(addr.get("ISO3166-2-lvl4") or addr.get("ISO3166-2-lvl3") or "").upper()
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
    try:
        lat, lng = float(hit["lat"]), float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        lat = lng = None
    return Place(city=str(city), region=region if region.startswith(country + "-") else "",
                 country=country, lat=lat, lng=lng)


def _bare_code(text: str) -> bool:
    parts = _tables()._parts(text)
    return len(parts) == 1 and parts[0].isalpha() and len(parts[0]) <= 3


async def resolve_text(state, text: str, geocode=geocode_place) -> tuple[Place | None, bool]:
    """Free text to a Place, cached forever: the tables, then (when they
    can't settle it) the geocoder. A bare code that is a country and a state
    too is never sent to the geocoder, which would only guess again: it
    stays unresolved for you (``set_place``)."""
    key = _cache_key(text)
    hit = await state.get_setting(key)
    if hit:
        return cached_from({key: hit}, text)
    place, ambiguous = place_from_text(text)
    if place or not ambiguous:
        return place, False
    if _bare_code(text) or geocode is None:
        await state.set_setting(key, UNRESOLVED)
        return None, True
    found = await geocode(text)
    # The geocoder must agree with something the text says: its country is
    # one of the readings the tables allowed ("PA": the US or Panama).
    readings = _readings(text)
    if found and found.known and (not readings or found.country in readings):
        await state.set_setting(key, found.to_json())
        return found, False
    await state.set_setting(key, UNRESOLVED)
    return None, True


def _readings(text: str) -> set[str]:
    """The countries an ambiguous text could be in, from its codes."""
    sw = _tables()
    out = set()
    for part in sw._parts(text):
        for code in sw._codes(part) or [part.upper()]:
            if code in sw.REGION_COUNTRY:
                out.add(sw.REGION_COUNTRY[code])
            if sw.country_code(code):
                out.add(sw.country_code(code))
    return out


async def set_place(state, text: str, place: Place) -> None:
    """You settle free text Sellbird couldn't ("DE" means Germany here)."""
    await state.set_setting(_cache_key(text), place.to_json())


def places_value(places: list[Place]) -> str:
    """Several places as one observation value (COMPANY_SITE)."""
    return json.dumps([json.loads(p.to_json()) for p in places], ensure_ascii=False)


def parse_places(value: str) -> list[Place | str]:
    """An observation value back to places. Older values are " | "-joined
    text, returned as strings for the caller to resolve."""
    text = str(value or "").strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            items = []
        return [p for p in (Place.from_json(i) for i in items if isinstance(i, dict)) if p]
    if text.startswith("{"):
        p = Place.from_json(text)
        return [p] if p else []
    return split_sites(text)


# ── Places from what you give Sellbird ──

LOCATION_COLUMNS = ("location", "person location", "contact location", "company location")
CITY_COLUMNS = ("city", "town", "locality")
REGION_COLUMNS = ("state", "region", "province", "state/province")
COUNTRY_COLUMNS = ("country", "country code", "country/region")


def _cells(row: dict) -> dict:
    return {str(k).strip().lower(): str(v or "").strip() for k, v in (row or {}).items() if k}


def _first(cells: dict, names) -> str:
    return next((cells[n] for n in names if cells.get(n)), "")


def row_place(row: dict) -> tuple[Place | None, str]:
    """A CSV row's place. Separate city, state and country columns map field
    by field ("DE" in a country column is Germany); a single location column
    is free text, returned as the second value for resolve_text."""
    cells = _cells(row)
    city, region, country = (_first(cells, CITY_COLUMNS), _first(cells, REGION_COLUMNS),
                             _first(cells, COUNTRY_COLUMNS))
    if city or region or country:
        return place_from_fields(city, region, country), ""
    return None, _first(cells, LOCATION_COLUMNS)


async def place_or_text(state, text: str, geocode=geocode_place) -> str:
    """What to store for free text you typed or a CSV gave: the resolved
    place, or the text itself when it can't be settled (it then shows as
    needing you)."""
    found, _ = await resolve_text(state, text, geocode=geocode)
    return found.to_json() if found else " ".join(text.split())
