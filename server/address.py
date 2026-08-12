"""Checking that a spoken address is a real place — and where it is.

WHY THIS EXISTS. Stanford measured fifteen speech models against two purpose-built
benchmarks of street names in February 2026 and found that **44% of street names
come back wrong on average**. The number that matters more: Whisper-Large has a
14% word error rate overall and a 27% error rate on street names specifically.
Aggregate accuracy hides exactly the tokens a dispatch depends on. Non-native
speakers fare worse still — 46% against 64% — and a mangled street sent the
technician an average of 2.4 miles away.

An agent cannot hear its way out of that. Reading the address back helps and does
not fix it, because the agent reads back what it misheard and the caller, who
knows their own address, hears what they expect. So the address is checked
against a real gazetteer, the same way the phone number is checked against
libphonenumber instead of against a regex someone wrote.

It is worth more than confirmation. Asked about "1200 Urving Street", the census
geocoder answers "1200 IRVING ST, SAN FRANCISCO, CA, 94122" — it repairs the
error rather than merely reporting it.

PROVIDERS. The default is the US Census Bureau geocoder: free, no key, no
billing account, no quota to sign up for. That matters for a project people
clone — a kit that demands a Google Cloud project before it can take a booking
has a worse first five minutes than one that just works. Google is available for
the cases the census data does not cover (it is US-only, and it knows address
ranges rather than every unit), and needs GOOGLE_MAPS_API_KEY.

    ADDRESS_PROVIDER=census   # default when the business country is US
    ADDRESS_PROVIDER=google   # needs GOOGLE_MAPS_API_KEY
    ADDRESS_PROVIDER=off      # no checking; the raw text is stored

FAILURE POLICY, and it goes in two directions on purpose:

  * The provider answered and found nothing → refuse. A booking with an address
    that does not exist is the wasted truck roll owners complain about most, and
    it is silent: the job looks fine until somebody is parked outside a building
    that isn't there.

  * The provider timed out, errored, or is unreachable → accept, and mark the
    address unverified. A 24/7 emergency service must not stop taking work
    because a government API is down. Losing every job for an hour is a worse
    failure than accepting one address that might be mistyped.

That asymmetry is the whole design. Anything else either drops jobs during an
outage or dispatches into the void.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

#: Seconds to wait for the geocoder. This runs inside a live phone call, so the
#: budget is small: the caller is listening to silence while it happens. Three
#: seconds is roughly one "let me check..." and the census service normally
#: answers in a few hundred milliseconds.
TIMEOUT = float(os.getenv("ADDRESS_TIMEOUT", "3"))

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
GOOGLE_URL = "https://addressvalidation.googleapis.com/v1:validateAddress"


@dataclass(frozen=True)
class AddressCheck:
    """What the gazetteer had to say.

    `reachable` and `verified` are separate because they lead to opposite
    decisions — see the failure policy above. Collapsing them into one boolean
    was the first version and it made an outage indistinguishable from a fake
    address.
    """

    raw: str
    canonical: str = ""  # the repaired, standardised form; "" when unknown
    city: str = ""
    postcode: str = ""
    verified: bool = False  # a real address was matched
    reachable: bool = True  # the provider answered at all

    @property
    def best(self) -> str:
        """What to store: the corrected address when there is one."""
        return self.canonical or self.raw


def provider_for(country: str) -> str:
    """Which provider to use, unless the environment overrides it.

    The census geocoder covers the United States and nothing else, so it is the
    default only where it can answer. Elsewhere checking is off rather than
    silently wrong — an agent that refuses every Ukrainian address because a US
    gazetteer has never heard of it would be worse than no checking at all.
    """
    explicit = os.getenv("ADDRESS_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if (country or "").upper() == "US":
        return "census"
    return "off"


def _get_json(url: str, params: dict) -> dict | None:
    """GET returning parsed JSON, or None for every kind of not-answering.

    Deliberately broad: a DNS failure, a 500, a timeout and a body that is not
    JSON all mean the same thing to the caller — no opinion available — and the
    booking must go through regardless.
    """
    full = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full, timeout=TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _post_json(url: str, params: dict, payload: dict) -> dict | None:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        full, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _check_census(raw: str) -> AddressCheck:
    data = _get_json(
        CENSUS_URL,
        {"address": raw, "benchmark": "Public_AR_Current", "format": "json"},
    )
    if data is None:
        return AddressCheck(raw=raw, reachable=False)
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return AddressCheck(raw=raw, verified=False, reachable=True)
    m = matches[0]
    comp = m.get("addressComponents") or {}
    return AddressCheck(
        raw=raw,
        canonical=m.get("matchedAddress", ""),
        city=comp.get("city", ""),
        postcode=comp.get("zip", ""),
        verified=True,
        reachable=True,
    )


def _check_google(raw: str, country: str) -> AddressCheck:
    key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        # Not an error the caller should suffer for: a misconfigured key means
        # no opinion, which is the same as the provider being down.
        return AddressCheck(raw=raw, reachable=False)
    data = _post_json(
        GOOGLE_URL,
        {"key": key},
        {"address": {"addressLines": [raw], "regionCode": (country or "US").upper()}},
    )
    if data is None:
        return AddressCheck(raw=raw, reachable=False)
    result = data.get("result") or {}
    verdict = result.get("verdict") or {}
    addr = result.get("address") or {}
    city = postcode = ""
    for c in addr.get("addressComponents") or []:
        kind = c.get("componentType")
        text = (c.get("componentName") or {}).get("text", "")
        if kind == "locality":
            city = text
        elif kind == "postal_code":
            postcode = text
    # `addressComplete` is Google's own word for "every component resolved and
    # nothing was guessed", which is the bar a dispatch needs.
    complete = bool(verdict.get("addressComplete"))
    return AddressCheck(
        raw=raw,
        canonical=addr.get("formattedAddress", ""),
        city=city,
        postcode=postcode,
        verified=complete,
        reachable=True,
    )


def _lookup(provider: str, raw: str, country: str) -> AddressCheck:
    if provider == "census":
        return _check_census(raw)
    if provider == "google":
        return _check_google(raw, country)
    return AddressCheck(raw=raw, reachable=False)  # off: no opinion either way


def check_address(raw: str | None, country: str = "US", hints: tuple = ()) -> AddressCheck:
    """Look an address up. Never raises: every failure is a field on the result.

    `hints` are place names to retry with when the bare text finds nothing —
    in practice the business's service area. The census matcher needs a city or
    a zip, and the model routinely passes the street alone even when the caller
    named the city two turns earlier: a live call spent five identical
    create_booking attempts on "1200 Irving Street" — a real address — because
    nothing ever appended the San Francisco the agent already knew.

    A hint already present in the text is skipped, and the retry only happens
    after the provider ANSWERED no-match: an outage stops the loop rather than
    multiplying its timeout by the number of hints. The residual risk — a street
    that exists in a hint city but was meant in an unnamed other one — is
    accepted: the recap still reads the result back to the caller, and the
    alternative is refusing every street-only address outright.
    """
    raw = (raw or "").strip()
    if not raw:
        return AddressCheck(raw="", verified=False, reachable=True)
    provider = provider_for(country)
    check = _lookup(provider, raw, country)
    if check.verified or not check.reachable:
        return check
    low = raw.lower()
    for hint in hints:
        h = str(hint or "").strip()
        if not h or h.lower() in low:
            continue
        retry = _lookup(provider, f"{raw}, {h}", country)
        if not retry.reachable:
            return check
        if retry.verified:
            return retry
    return check


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace(",", " ").split())


def in_service_area(check: AddressCheck, area: tuple[str, ...]) -> bool:
    """Whether a verified address falls inside the places this business covers.

    `area` holds city names, postcodes, or both — whichever the owner finds
    natural to write. Matching is done on the CANONICAL city and postcode, not
    on what the caller said, which is the point: "San Fran", "SF" and a
    misheard street all reduce to the same "SAN FRANCISCO" first.

    An unverified address returns True. It is not evidence of being outside the
    area, and refusing on the strength of an outage would turn a geocoder
    hiccup into a lost job.
    """
    if not area or not check.verified:
        return True
    wanted = {_norm(a) for a in area if str(a).strip()}
    return _norm(check.city) in wanted or _norm(check.postcode) in wanted
