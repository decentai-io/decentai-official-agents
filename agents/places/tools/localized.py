"""Which language Google answers in, and which region it favours: given, or taken from the request.

- **Language:** the ``language`` input (BCP-47, such as ``ar`` or ``en-GB``); else the script the person wrote in,
  where that script names a language (Arabic script is Arabic, unless Persian or Urdu letters say otherwise);
  else none, and Google answers in its default (English).
- **Region:** the ``region`` input (two letters, such as ``AE``); else the country of the person's ``timezone``
  (IANA) where it is one this agent knows; else none, and Google decides.

Nothing is guessed beyond that: a request in Latin letters with no zone is sent with neither, and the answer says
which were used.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .maps_api import LANGUAGE, REGION

SCRIPTS = [
    (re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]"), "ar"),
    (re.compile(r"[֐-׿]"), "he"),
    (re.compile(r"[Ͱ-Ͽ]"), "el"),
    (re.compile(r"[฀-๿]"), "th"),
    (re.compile(r"[가-힯]"), "ko"),
    (re.compile(r"[぀-ヿ]"), "ja"),
]
PERSIAN = re.compile(r"[پچژگکی]")
URDU = re.compile(r"[ٹڈڑںےھ]")

#: IANA zones to the country they are in (zone.tab), for the zones people here are most likely in.
ZONES = {
    "Asia/Dubai": "AE", "Asia/Riyadh": "SA", "Asia/Qatar": "QA", "Asia/Bahrain": "BH", "Asia/Kuwait": "KW",
    "Asia/Muscat": "OM", "Asia/Baghdad": "IQ", "Asia/Amman": "JO", "Asia/Beirut": "LB", "Asia/Damascus": "SY",
    "Asia/Jerusalem": "IL", "Asia/Gaza": "PS", "Asia/Hebron": "PS", "Asia/Aden": "YE", "Asia/Tehran": "IR",
    "Asia/Karachi": "PK", "Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Asia/Dhaka": "BD", "Asia/Kathmandu": "NP",
    "Asia/Colombo": "LK", "Asia/Singapore": "SG", "Asia/Kuala_Lumpur": "MY", "Asia/Jakarta": "ID",
    "Asia/Manila": "PH", "Asia/Bangkok": "TH", "Asia/Hong_Kong": "HK", "Asia/Shanghai": "CN", "Asia/Tokyo": "JP",
    "Asia/Seoul": "KR", "Asia/Istanbul": "TR", "Europe/Istanbul": "TR", "Africa/Cairo": "EG",
    "Africa/Casablanca": "MA", "Africa/Algiers": "DZ", "Africa/Tunis": "TN", "Africa/Tripoli": "LY",
    "Africa/Khartoum": "SD", "Africa/Nairobi": "KE", "Africa/Lagos": "NG", "Africa/Johannesburg": "ZA",
    "Europe/London": "GB", "Europe/Dublin": "IE", "Europe/Paris": "FR", "Europe/Berlin": "DE",
    "Europe/Madrid": "ES", "Europe/Rome": "IT", "Europe/Amsterdam": "NL", "Europe/Brussels": "BE",
    "Europe/Zurich": "CH", "Europe/Vienna": "AT", "Europe/Stockholm": "SE", "Europe/Oslo": "NO",
    "Europe/Copenhagen": "DK", "Europe/Warsaw": "PL", "Europe/Athens": "GR", "Europe/Lisbon": "PT",
    "Europe/Moscow": "RU", "America/New_York": "US", "America/Chicago": "US", "America/Denver": "US",
    "America/Los_Angeles": "US", "America/Phoenix": "US", "America/Toronto": "CA", "America/Vancouver": "CA",
    "America/Mexico_City": "MX", "America/Sao_Paulo": "BR", "Australia/Sydney": "AU",
    "Australia/Melbourne": "AU", "Pacific/Auckland": "NZ",
}


def written_in(texts: Iterable[Any]) -> str:
    """The language a script names, from what the person wrote; "" for Latin letters or a mix of scripts."""
    text = " ".join(str(t or "") for t in texts)
    found = {code for pattern, code in SCRIPTS if pattern.search(text)}
    if len(found) != 1:
        return ""
    code = found.pop()
    if code == "ar":
        return "ur" if URDU.search(text) else "fa" if PERSIAN.search(text) else "ar"
    return code


def choose(inputs: dict[str, Any], texts: Iterable[Any]) -> tuple[str, str, str]:
    """(language, region, a refusal): each "" when none is sent."""
    language = str(inputs.get("language") or "").strip()
    region = str(inputs.get("region") or "").strip()
    if language and not LANGUAGE.match(language):
        return "", "", f"language is a BCP-47 code such as ar or en-GB, not {language!r}."
    if region and not REGION.match(region):
        return "", "", f"region is a two-letter country code such as AE, not {region!r}."
    zone = str(inputs.get("timezone") or "").strip()
    return language or written_in(texts), (region or ZONES.get(zone, "")).upper(), ""


def routes_region(region: str) -> str:
    """The Routes API takes a ccTLD, where the Places API takes a CLDR code: the United Kingdom is uk there."""
    return {"GB": "uk"}.get(region.upper(), region.lower()) if region else ""
