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


def _format(address: dict) -> str:
    """"Seattle, WA, US" from a PostalAddress, or "" without a locality or
    region to place it by."""
    parts = [_text(address.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")]
    if not (parts[0] or parts[1]):
        return ""
    return ", ".join(p for p in parts if p)


def _walk(data, out: list[str]) -> None:
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
        site = _format(data)
        if site:
            out.append(site)
        return
    for key, value in data.items():
        if key == "address" and isinstance(value, str):
            continue  # a one-line street address can't be placed reliably
        _walk(value, out)


def sites_from_schema(html: str) -> list[str]:
    """The distinct sites a page's JSON-LD gives, in page order."""
    found: list[str] = []
    for block in _json_blocks(html):
        _walk(block, found)
    seen, out = set(), []
    for site in found:
        key = site.lower()
        if key not in seen:
            seen.add(key)
            out.append(site)
    return out


def join_sites(sites: list[str]) -> str:
    return SITE_SEPARATOR.join(sites)


def split_sites(text: str) -> list[str]:
    return [s.strip() for s in str(text or "").split(SITE_SEPARATOR.strip()) if s.strip()]
