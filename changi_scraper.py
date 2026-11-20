#!/usr/bin/env python3
"""
Changi Airport departures (any terminal) — by destination country, in 2-hour intervals.

Reporting window: 06:00 on the selected date up to (not including) 02:00 on the
following calendar day, Singapore time (Asia/Singapore, UTC+8).

Usage (Windows, from the project folder):
    python changi_scraper.py                       # today in Singapore, headless
    python changi_scraper.py --date 2026-10-14     # a specific date
    python changi_scraper.py --visible             # watch Chrome work
    python changi_scraper.py --terminal 3          # Terminal 3 instead of Terminal 1
    python changi_scraper.py --inspect --visible   # DOM diagnostics only (no report)
    python changi_scraper.py --replay-raw output/raw --date 2026-10-14
                                                      # re-parse saved text, no browser

Design notes
------------
* No CSS class names or hard-coded XPaths for the site are used. The live DOM
  could not be inspected while writing this (changiairport.com was outside the
  build sandbox's network allow-list), so every page element is located by
  behaviour and visible text:
    - flight rows: the smallest element containing a "City (IATA)" text, an
      HH:MM time and a flight number;
    - date-picker trigger: a visible element whose own text is a date (e.g.
      "Sat, 10 Oct 2026") or whose aria-label/title mentions date/calendar, and
      which is NOT inside a link and NOT inside a flight row (this is what
      previously sent the scraper to a flight-details page);
    - calendar: a visible "OCT 2026"-style header whose nearest ancestor holds
      28+ day-number cells; arrows are found by aria-label or by position on the
      header's row; day cells are generic elements (div/span/td/button).
  Shadow roots (open) are traversed; same-origin iframes are searched if the
  calendar is not in the main document.
* Every click is followed by a navigation guard: if the URL leaves the
  departures listing or a new tab opens, the scraper recovers or stops with an
  error instead of scraping the wrong page.
* Each selected date is verified after selection (displayed date text, or the
  calendar's selected cell), and the listing must refresh before scraping.
* Text parsing (the approach of the original script) is kept, but now runs per
  detected row, so status/estimated times stay attached to the right flight.

See README.md for the full explanation, debugging procedure and limitations.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

try:  # Selenium is only needed for live scraping; parsing/tests work without it.
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchFrameException,
        StaleElementReferenceException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:  # pragma: no cover
    webdriver = None

try:  # Optional airport reference dataset (pip install airportsdata)
    import airportsdata  # type: ignore
except ImportError:  # pragma: no cover
    airportsdata = None


SGT = ZoneInfo("Asia/Singapore")
URL_TEMPLATE = "https://www.changiairport.com/en/fly/flight-information/departures.html?terminal={terminal}"
TERMINALS = ("1", "2", "3", "4")
LOG = logging.getLogger("changi")

ROW_START = "<<<ROW>>>"
ROW_END = "<<<END>>>"


# =============================================================================
# Errors
# =============================================================================
class ScraperError(RuntimeError):
    """Base class for scraping failures that should stop the run."""


class PageNavigationError(ScraperError):
    """The browser left the departures listing (e.g. opened a flight-details page)."""


class CalendarError(ScraperError):
    """The date picker could not be found, operated or verified."""


class ListingLoadError(ScraperError):
    """The flight listing did not load or could not be read."""


class IncompleteDataError(ScraperError):
    """Data for the reporting window could not be fully collected."""


# =============================================================================
# Configuration
# =============================================================================
@dataclass
class Config:
    report_date: date
    url: Optional[str] = None           # explicit page URL (tests); default built from terminal
    terminal: str = "1"
    headless: bool = True
    output_dir: Path = Path("output")
    cancelled_policy: str = "exclude"   # "exclude" | "include"
    time_basis: str = "scheduled"       # "scheduled" | "estimated"
    codeshare_mode: str = "auto"        # "auto" | "strict"
    allow_partial: bool = False
    doc_format: str = "both"            # "both" | "docx" | "pdf" | "none"
    page_timeout: int = 45              # seconds to wait for the listing to appear
    max_show_more_clicks: int = 40
    chrome_binary: Optional[str] = None
    chromedriver: Optional[str] = None
    browser: str = "auto"               # "auto" (Chrome, else Edge) | "chrome" | "edge"

    @property
    def site_url(self) -> str:
        return self.url or URL_TEMPLATE.format(terminal=self.terminal)

    @property
    def file_prefix(self) -> str:
        return f"changi_t{self.terminal}"


# =============================================================================
# Reporting window and intervals (timezone-aware, real calendar dates)
# =============================================================================
@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime  # exclusive

    @property
    def label(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M}"

    def contains(self, dt: datetime) -> bool:
        return self.start <= dt < self.end


def reporting_window(report_date: date) -> tuple[datetime, datetime]:
    """06:00 on report_date (inclusive) to 02:00 on the next day (exclusive), SGT."""
    start = datetime.combine(report_date, dtime(6, 0), tzinfo=SGT)
    end = datetime.combine(report_date + timedelta(days=1), dtime(2, 0), tzinfo=SGT)
    return start, end


def build_intervals(report_date: date) -> list[Interval]:
    start, end = reporting_window(report_date)
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(hours=2), end)
        out.append(Interval(cur, nxt))
        cur = nxt
    return out  # 10 intervals: 06-08 ... 22-00, 00-02 (next day)


def interval_for(dt: datetime, intervals: list[Interval]) -> Optional[Interval]:
    for iv in intervals:
        if iv.contains(dt):
            return iv
    return None


def today_in_singapore() -> date:
    return datetime.now(SGT).date()


# =============================================================================
# Destination → country mapping
# =============================================================================
# Manual IATA airport → ISO 3166-1 alpha-2 fallback (used when the airportsdata
# package is unavailable or doesn't know a code). Compact "IATA:CC" format.
_IATA_FALLBACK_RAW = """
KUL:MY PEN:MY BKI:MY KCH:MY LGK:MY JHB:MY SZB:MY MYY:MY SBW:MY TWU:MY IPH:MY
KBR:MY TGG:MY LBU:MY SDK:MY
BKK:TH DMK:TH HKT:TH CNX:TH KBV:TH HDY:TH USM:TH UTP:TH CEI:TH
CGK:ID HLP:ID DPS:ID SUB:ID KNO:ID JOG:ID YIA:ID UPG:ID BTH:ID PDG:ID PLM:ID
PKU:ID BPN:ID SRG:ID LOP:ID BDO:ID SOC:ID MDC:ID PNK:ID TJQ:ID TNJ:ID
MNL:PH CEB:PH DVO:PH CRK:PH ILO:PH KLO:PH PPS:PH
SGN:VN HAN:VN DAD:VN PQC:VN CXR:VN HPH:VN VCA:VN
PNH:KH REP:KH SAI:KH KTI:KH VTE:LA LPQ:LA RGN:MM MDL:MM BWN:BN DIL:TL
HKG:HK MFM:MO TPE:TW TSA:TW KHH:TW RMQ:TW
PEK:CN PKX:CN PVG:CN SHA:CN CAN:CN SZX:CN CTU:CN TFU:CN XMN:CN HGH:CN NKG:CN
TAO:CN WUH:CN CKG:CN XIY:CN KMG:CN TSN:CN FOC:CN HAK:CN SYX:CN CSX:CN NNG:CN
KWL:CN KWE:CN CGO:CN TYN:CN SHE:CN DLC:CN HRB:CN SJW:CN HFE:CN NGB:CN WNZ:CN
JJN:CN SWA:CN ZUH:CN
NRT:JP HND:JP KIX:JP ITM:JP NGO:JP FUK:JP CTS:JP OKA:JP
ICN:KR GMP:KR PUS:KR CJU:KR UBN:MN ULN:MN
DEL:IN BOM:IN MAA:IN BLR:IN HYD:IN CCU:IN COK:IN AMD:IN ATQ:IN CJB:IN TRV:IN
TRZ:IN GOI:IN GOX:IN PNQ:IN VTZ:IN LKO:IN JAI:IN
CMB:LK DAC:BD CGP:BD KTM:NP MLE:MV KHI:PK LHE:PK ISB:PK
PER:AU SYD:AU MEL:AU BNE:AU ADL:AU DRW:AU OOL:AU CNS:AU CBR:AU HBA:AU AVV:AU
AKL:NZ CHC:NZ WLG:NZ ZQN:NZ NAN:FJ POM:PG
DXB:AE AUH:AE SHJ:AE DWC:AE DOH:QA IST:TR SAW:TR CAI:EG RUH:SA JED:SA DMM:SA
MED:SA AMM:JO TLV:IL MCT:OM BAH:BH KWI:KW
LHR:GB LGW:GB MAN:GB STN:GB LCY:GB CDG:FR ORY:FR FRA:DE MUC:DE BER:DE AMS:NL
ZRH:CH GVA:CH FCO:IT MXP:IT MAD:ES BCN:ES CPH:DK SVO:RU DME:RU ARN:SE OSL:NO
HEL:FI VIE:AT BRU:BE ATH:GR LIS:PT DUB:IE
SFO:US LAX:US JFK:US EWR:US IAH:US SEA:US HNL:US YVR:CA YYZ:CA
JNB:ZA CPT:ZA NBO:KE MRU:MU SEZ:SC ADD:ET
"""
IATA_TO_COUNTRY: dict[str, str] = dict(
    tok.split(":") for tok in _IATA_FALLBACK_RAW.split() if ":" in tok
)

# City (normalised, lowercase, no accents) → ISO alpha-2. Retained from the
# original script and expanded with alternative spellings / airport names.
CITY_TO_COUNTRY: dict[str, str] = {
    # Southeast Asia
    "kuala lumpur": "MY", "penang": "MY", "kota kinabalu": "MY", "langkawi": "MY",
    "johor bahru": "MY", "kuching": "MY", "kota bharu": "MY", "sibu": "MY", "miri": "MY",
    "sandakan": "MY", "tawau": "MY", "ipoh": "MY", "kuala terengganu": "MY",
    "labuan": "MY", "subang": "MY",
    "bangkok": "TH", "bangkok suvarnabhumi": "TH", "bangkok don mueang": "TH",
    "phuket": "TH", "chiang mai": "TH", "chiang rai": "TH", "krabi": "TH",
    "hat yai": "TH", "koh samui": "TH", "samui": "TH", "u-tapao": "TH", "pattaya": "TH",
    "jakarta": "ID", "denpasar": "ID", "bali": "ID", "denpasar bali": "ID",
    "bali denpasar": "ID", "surabaya": "ID", "medan": "ID", "yogyakarta": "ID",
    "makassar": "ID", "ujung pandang": "ID", "batam": "ID", "padang": "ID",
    "palembang": "ID", "pekanbaru": "ID", "balikpapan": "ID", "semarang": "ID",
    "lombok": "ID", "mataram": "ID", "bandung": "ID", "manado": "ID", "pontianak": "ID",
    "tanjung pinang": "ID", "bintan": "ID",
    "manila": "PH", "cebu": "PH", "davao": "PH", "clark": "PH", "iloilo": "PH",
    "kalibo": "PH", "boracay": "PH", "puerto princesa": "PH",
    "ho chi minh city": "VN", "ho chi minh": "VN", "saigon": "VN", "hanoi": "VN",
    "da nang": "VN", "danang": "VN", "phu quoc": "VN", "nha trang": "VN",
    "cam ranh": "VN", "hai phong": "VN", "haiphong": "VN", "can tho": "VN",
    "phnom penh": "KH", "siem reap": "KH", "sihanoukville": "KH",
    "vientiane": "LA", "luang prabang": "LA",
    "yangon": "MM", "rangoon": "MM", "mandalay": "MM",
    "brunei": "BN", "bandar seri begawan": "BN",
    "dili": "TL",
    # Greater China / NE Asia
    "hong kong": "HK", "hong kong sar": "HK", "macau": "MO", "macao": "MO",
    "taipei": "TW", "taipei taoyuan": "TW", "taipei songshan": "TW",
    "kaohsiung": "TW", "taichung": "TW",
    "beijing": "CN", "beijing capital": "CN", "beijing daxing": "CN",
    "shanghai": "CN", "shanghai pudong": "CN", "shanghai hongqiao": "CN",
    "guangzhou": "CN", "shenzhen": "CN", "chengdu": "CN", "chengdu tianfu": "CN",
    "xiamen": "CN", "hangzhou": "CN", "nanjing": "CN", "qingdao": "CN", "wuhan": "CN",
    "chongqing": "CN", "xi'an": "CN", "xian": "CN", "kunming": "CN", "tianjin": "CN",
    "fuzhou": "CN", "haikou": "CN", "sanya": "CN", "changsha": "CN", "nanning": "CN",
    "guilin": "CN", "guiyang": "CN", "zhengzhou": "CN", "taiyuan": "CN",
    "shenyang": "CN", "dalian": "CN", "harbin": "CN", "shijiazhuang": "CN",
    "hefei": "CN", "ningbo": "CN", "wenzhou": "CN", "quanzhou": "CN", "jinjiang": "CN",
    "shantou": "CN", "zhuhai": "CN",
    "tokyo": "JP", "tokyo narita": "JP", "tokyo haneda": "JP", "narita": "JP",
    "haneda": "JP", "osaka": "JP", "osaka kansai": "JP", "nagoya": "JP",
    "fukuoka": "JP", "sapporo": "JP", "okinawa": "JP", "naha": "JP",
    "seoul": "KR", "seoul incheon": "KR", "incheon": "KR", "busan": "KR", "jeju": "KR",
    "ulaanbaatar": "MN",
    # South Asia
    "delhi": "IN", "new delhi": "IN", "mumbai": "IN", "chennai": "IN", "bengaluru": "IN",
    "bangalore": "IN", "hyderabad": "IN", "kolkata": "IN", "kochi": "IN", "cochin": "IN",
    "ahmedabad": "IN", "amritsar": "IN", "coimbatore": "IN", "trivandrum": "IN",
    "thiruvananthapuram": "IN", "tiruchirappalli": "IN", "trichy": "IN", "goa": "IN",
    "pune": "IN", "visakhapatnam": "IN", "lucknow": "IN", "jaipur": "IN",
    "colombo": "LK", "dhaka": "BD", "chittagong": "BD", "chattogram": "BD",
    "kathmandu": "NP", "male": "MV", "karachi": "PK", "lahore": "PK", "islamabad": "PK",
    # Oceania
    "perth": "AU", "sydney": "AU", "melbourne": "AU", "brisbane": "AU",
    "adelaide": "AU", "darwin": "AU", "gold coast": "AU", "cairns": "AU",
    "canberra": "AU", "hobart": "AU", "avalon": "AU",
    "auckland": "NZ", "christchurch": "NZ", "wellington": "NZ", "queenstown": "NZ",
    "nadi": "FJ", "port moresby": "PG",
    # Middle East
    "dubai": "AE", "abu dhabi": "AE", "sharjah": "AE", "doha": "QA",
    "istanbul": "TR", "cairo": "EG", "riyadh": "SA", "jeddah": "SA", "dammam": "SA",
    "medina": "SA", "madinah": "SA", "amman": "JO", "tel aviv": "IL", "muscat": "OM",
    "bahrain": "BH", "kuwait": "KW",
    # Europe
    "london": "GB", "london heathrow": "GB", "london gatwick": "GB", "manchester": "GB",
    "paris": "FR", "frankfurt": "DE", "munich": "DE", "berlin": "DE",
    "amsterdam": "NL", "zurich": "CH", "geneva": "CH", "rome": "IT", "milan": "IT",
    "madrid": "ES", "barcelona": "ES", "copenhagen": "DK", "moscow": "RU",
    "stockholm": "SE", "oslo": "NO", "helsinki": "FI", "vienna": "AT",
    "brussels": "BE", "athens": "GR", "lisbon": "PT", "dublin": "IE",
    # Americas
    "san francisco": "US", "los angeles": "US", "new york": "US", "newark": "US",
    "houston": "US", "seattle": "US", "honolulu": "US",
    "vancouver": "CA", "toronto": "CA",
    # Africa
    "johannesburg": "ZA", "cape town": "ZA", "nairobi": "KE", "addis ababa": "ET",
    "mauritius": "MU", "port louis": "MU", "seychelles": "SC", "mahe": "SC",
}

_AIRPORT_WORDS = re.compile(
    r"\b(international|intl|airport|apt|terminal \d)\b", re.IGNORECASE
)


def normalise_city(city: str) -> str:
    s = unicodedata.normalize("NFKD", city or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'")
    s = re.sub(r"\(.*?\)", " ", s)
    s = _AIRPORT_WORDS.sub(" ", s)
    s = re.sub(r"[^a-z' \-]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


class CountryResolver:
    """IATA code first (reference dataset, then manual table), city name second."""

    def __init__(self, use_dataset: bool = True):
        self.dataset: dict = {}
        if use_dataset and airportsdata is not None:
            try:
                self.dataset = airportsdata.load("IATA")
            except Exception as exc:  # pragma: no cover
                LOG.warning("Could not load airportsdata (%s); using manual table", exc)
        elif use_dataset:
            LOG.info("airportsdata not installed; using manual IATA/city tables only")
        self.unknown: Counter = Counter()

    def resolve(self, city: str, code: str) -> tuple[str, str]:
        """Return (ISO alpha-2 country, source). Unknown → ('??', 'unknown')."""
        code = (code or "").strip().upper()
        by_city = self._by_city(city)
        if re.fullmatch(r"[A-Z]{3}", code):
            rec = self.dataset.get(code)
            if rec and rec.get("country"):
                cc = rec["country"].upper()
                if by_city and by_city != cc:
                    LOG.warning("Country mismatch for %s (%s): IATA dataset=%s, city table=%s;"
                                " using IATA dataset", city, code, cc, by_city)
                return cc, "iata-dataset"
            if code in IATA_TO_COUNTRY:
                return IATA_TO_COUNTRY[code], "iata-table"
        if by_city:
            return by_city, "city-table"
        self.unknown[f"{city} ({code})"] += 1
        return "??", "unknown"

    @staticmethod
    def _by_city(city: str) -> Optional[str]:
        key = normalise_city(city)
        if not key:
            return None
        if key in CITY_TO_COUNTRY:
            return CITY_TO_COUNTRY[key]
        base = re.split(r"[,/]| - ", key)[0].strip()
        if base in CITY_TO_COUNTRY:
            return CITY_TO_COUNTRY[base]
        # Longest known city name that prefixes the text, e.g. "bangkok suvarnabhumi x"
        for name in sorted(CITY_TO_COUNTRY, key=len, reverse=True):
            if key.startswith(name + " "):
                return CITY_TO_COUNTRY[name]
        return None


# =============================================================================
# Text parsing
# =============================================================================
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DAYNAME = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+"
_DATE_PATTERNS = [
    re.compile(rf"^(?:{_DAYNAME})?(?P<d>\d{{1,2}})\s+(?P<mon>[a-z]{{3,9}})\.?,?(?:\s+(?P<y>\d{{4}}))?$", re.I),
    re.compile(rf"^(?:{_DAYNAME})?(?P<mon>[a-z]{{3,9}})\.?\s+(?P<d>\d{{1,2}}),?(?:\s+(?P<y>\d{{4}}))?$", re.I),
    re.compile(r"^(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})$"),
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$"),
]
_DATE_SEARCH = re.compile(
    r"(?P<d1>\d{1,2})\s+(?P<mon1>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?(?:\s+(?P<y1>\d{4}))?"
    r"|(?P<mon2>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<d2>\d{1,2})(?:,?\s+(?P<y2>\d{4}))?"
    r"|(?P<d3>\d{1,2})/(?P<m3>\d{1,2})/(?P<y3>\d{4})",
    re.I,
)
_DATE_PREFIX = re.compile(r"^(?:today|tomorrow|yesterday|departures?(?:\s+on)?)\s*[,·:\-–]?\s*", re.I)

_TIME_LINE = re.compile(
    r"^(?P<prefix>[A-Za-z][A-Za-z .\-/]{0,24}?)?\s*"
    r"(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)"
    r"\s*(?P<suffix>.{0,20})$"
)
_EST_PREFIX = re.compile(
    r"est|expected|new|retime|resched|revised|delay|depart|actual|at\b|now|gate|board", re.I
)
_FLIGHT_TOKEN = re.compile(
    r"(?P<des>[A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(?P<num>\d{1,4})(?:(?P<suf>[A-Z])(?![A-Za-z]))?"
)
_SEP_CHARS = " \t,/·|&+•-–"
# City may carry its own bracketed qualifier: "Tokyo (Narita) (NRT)" (as on the live site, Oct 2026)
_DEST = re.compile(r"(?P<city>[^()\n]*?[A-Za-zÀ-ÿ][^()\n]*?(?:\s*\([^()\n]*[a-z][^()\n]*\))?)\s*\((?P<code>[A-Z]{3})\)")
_BARE_CODE = re.compile(r"^\(?(?P<code>[A-Z]{3})\)?$")
_VIA = re.compile(r"^via\b\s*(?P<rest>.*)$", re.I)
_TERMINAL = re.compile(r"^(?:T|Terminal\s*)(?P<t>[1-4])$", re.I)
_GATE = re.compile(r"^Gate\s*:?\s*(?P<g>[A-Z]?\d{1,3}[A-Z]?)$", re.I)
_BARE_GATE = re.compile(r"^(?P<g>[A-F]\d{1,2}[A-Z]?)$")
_CHECKIN = re.compile(r"^(?:check[- ]?in(?:\s+(?:row|counter|aisle))?|row)\s*:?\s*(?P<c>[A-Z0-9\-]{1,8})$", re.I)
_OPERATED_BY = re.compile(r"operated by\s+(?P<a>.+)$", re.I)
_NOISE = re.compile(r"^show (?:earlier|later) flights$", re.I)
_GATE_INFO = re.compile(r"^gate info(?:rmation)?\b", re.I)   # "GATE INFO AT 20:30" = when gate is announced, not a departure time
_CODESHARE_LABEL = re.compile(r"^code[\s-]?shares?\s*:?$", re.I)
_ROW_DATE = re.compile(r"^<<<DATE:(\d{4}-\d{2}-\d{2})>>>$")
_LINK_DATE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")

STATUS_CANON = [
    (re.compile(r"cancel", re.I), "Cancelled"),
    (re.compile(r"retime|resched|new time|revised", re.I), "Retimed"),
    (re.compile(r"delay", re.I), "Delayed"),
    (re.compile(r"divert", re.I), "Diverted"),
    (re.compile(r"departed|took off|airborne", re.I), "Departed"),
    (re.compile(r"last call|final call", re.I), "Last Call"),
    (re.compile(r"boarding", re.I), "Boarding"),
    (re.compile(r"gate clos", re.I), "Gate Closed"),
    (re.compile(r"gate open", re.I), "Gate Open"),
    (re.compile(r"on time|on-time", re.I), "On Time"),
    (re.compile(r"^scheduled$|^confirmed$", re.I), "Scheduled"),
]


def canonical_status(text: str) -> Optional[str]:
    for rx, name in STATUS_CANON:
        if rx.search(text):
            return name
    return None


def parse_date_text(text: str, ref: date, *, full_line: bool = False) -> Optional[date]:
    """Parse a displayed date. Missing year → the year that puts it closest to ref."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    s = _DATE_PREFIX.sub("", s)
    groups = None
    if full_line:
        for rx in _DATE_PATTERNS:
            m = rx.match(s)
            if m:
                groups = m.groupdict()
                break
    else:
        m = _DATE_SEARCH.search(s)
        if m:
            g = m.groupdict()
            if g["d1"]:
                groups = {"d": g["d1"], "mon": g["mon1"], "y": g["y1"]}
            elif g["d2"]:
                groups = {"d": g["d2"], "mon": g["mon2"], "y": g["y2"]}
            else:
                groups = {"d": g["d3"], "m": g["m3"], "y": g["y3"]}
    if not groups:
        return None
    try:
        day = int(groups["d"])
        if groups.get("mon"):
            month = MONTHS.get(groups["mon"][:3].lower())
            if not month:
                return None
        else:
            month = int(groups["m"])
        if groups.get("y"):
            return date(int(groups["y"]), month, day)
        candidates = []
        for y in (ref.year - 1, ref.year, ref.year + 1):
            try:
                candidates.append(date(y, month, day))
            except ValueError:
                pass
        return min(candidates, key=lambda d: abs((d - ref).days)) if candidates else None
    except (ValueError, TypeError):
        return None


def parse_time_line(line: str) -> Optional[dict]:
    """'17:15' / 'Est 18:30' / 'Departed 17:22' / '00:40 (+1)' → dict, else None."""
    if len(line) > 48 or _DEST.search(line):
        return None
    m = _TIME_LINE.match(line.strip())
    if not m:
        return None
    prefix = (m.group("prefix") or "").strip()
    suffix = (m.group("suffix") or "").strip()
    if prefix and _FLIGHT_TOKEN.match(prefix.replace(" ", "")):
        return None
    t = dtime(int(m.group("h")), int(m.group("m")))
    status = canonical_status(prefix) or canonical_status(suffix)
    explicit_est = bool(prefix and _EST_PREFIX.search(prefix)) or bool(
        suffix and re.search(r"est|expected|new|retime", suffix, re.I))
    return {
        "time": t,
        "explicit_est": explicit_est,
        "status": status,
        "plus_one": bool(re.search(r"\+\s*1", suffix)),
    }


def parse_flight_line(line: str) -> Optional[tuple[list[str], str]]:
    """'TR416Scoot' / 'TR416 · Scoot' / 'SQ 106, LH 9771 Singapore Airlines'."""
    s = line.strip()
    if _BARE_GATE.match(s) or re.fullmatch(r"[A-Z]\d{1,3}", s):
        return None  # looks like a gate (e.g. C22, D41), not a flight
    pos, numbers = 0, []
    while True:
        m = _FLIGHT_TOKEN.match(s, pos)
        if not m:
            break
        des, num, suf = m.group("des"), m.group("num"), m.group("suf") or ""
        nxt = s[m.end(): m.end() + 1]
        if nxt and not (nxt in _SEP_CHARS or nxt.isalpha() or nxt == "("):
            break
        if not des.isalpha() and len(num) < 2:
            break  # 'C2 2' style gate fragments
        numbers.append(f"{des}{num}{suf}")
        pos = m.end()
        while pos < len(s) and s[pos] in _SEP_CHARS:
            pos += 1
        if not _FLIGHT_TOKEN.match(s, pos):
            break
    if not numbers:
        return None
    airline = s[pos:].strip(_SEP_CHARS + "()")
    if parse_time_line(airline or "x") or _DEST.search(airline):
        return None
    return numbers, airline


_LEAD_TIME = re.compile(r"^((?:[01]?\d|2[0-3]):[0-5]\d)\s*(.+)$")


def split_combined_line(line: str) -> list[str]:
    """'17:15TR416Scoot' / '17:15 TR416 · Scoot' → ['17:15', 'TR416Scoot'] (inline
    elements can put the time and flight on one rendered line)."""
    m = _LEAD_TIME.match(line)
    if m and (parse_flight_line(m.group(2)) or _DEST.search(m.group(2))):
        return [m.group(1)] + split_combined_line(m.group(2))
    return [line]


@dataclass
class RawFlight:
    order: int
    sched_time: Optional[dtime] = None
    time_inherited: bool = False
    est_time: Optional[dtime] = None
    flight_numbers: list[str] = field(default_factory=list)
    airline: str = ""
    operating_airline: str = ""
    dest_city: str = ""
    dest_code: str = ""
    via: list[str] = field(default_factory=list)
    status: str = ""
    terminal: str = ""
    gate: str = ""
    checkin: str = ""
    explicit_date: Optional[date] = None
    date_from_link: bool = False
    plus_one: bool = False
    from_row: bool = False
    raw: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.flight_numbers and self.dest_code and self.sched_time)


@dataclass
class ParseResult:
    flights: list[RawFlight]
    unparsed_rows: list[str]
    row_blocks: int


class ListingParser:
    """Parses annotated listing text (row blocks between ROW_START/ROW_END, plus
    free text). Free text uses the original script's sequential heuristics."""

    def __init__(self, page_date: date):
        self.page_date = page_date
        self.flights: list[RawFlight] = []
        self.unparsed: list[str] = []
        self.order = 0
        self.date_label: Optional[date] = None
        self.last_time: Optional[dtime] = None
        self.row_blocks = 0

    # ---- public -------------------------------------------------------------
    def parse(self, text: str) -> ParseResult:
        lines = []
        for ln in (text or "").splitlines():
            if ln.strip():
                lines.extend(split_combined_line(ln.strip()))
        free: list[str] = []
        i = 0
        while i < len(lines):
            if lines[i] == ROW_START:
                self._parse_free(free)
                free = []
                j = i + 1
                block = []
                while j < len(lines) and lines[j] != ROW_END:
                    block.append(lines[j])
                    j += 1
                self.row_blocks += 1
                self._parse_row(block)
                i = j + 1
            else:
                free.append(lines[i])
                i += 1
        self._parse_free(free)
        return ParseResult(self.flights, self.unparsed, self.row_blocks)

    # ---- helpers -------------------------------------------------------------
    def _new(self, **kw) -> RawFlight:
        self.order += 1
        return RawFlight(order=self.order, explicit_date=self.date_label, **kw)

    def _emit(self, rf: Optional[RawFlight]) -> None:
        if rf is None:
            return
        if rf.sched_time is None and rf.flight_numbers and rf.dest_code and self.last_time:
            rf.sched_time, rf.time_inherited = self.last_time, True
        if rf.complete:
            self.flights.append(rf)
            self.last_time = rf.sched_time
        elif rf.flight_numbers or rf.dest_code:
            self.unparsed.append(" | ".join(rf.raw))

    def _classify(self, line: str) -> tuple[str, object]:
        if _NOISE.match(line) or _GATE_INFO.match(line):
            return "noise", None
        m = _ROW_DATE.match(line)
        if m:
            return "rowdate", date.fromisoformat(m.group(1))
        if _CODESHARE_LABEL.match(line):
            return "codeshare", None
        m = re.match(r"^code[\s-]?shares?\s*:?\s*(?P<rest>.+)$", line, re.I)
        if m:
            fl = parse_flight_line(m.group("rest"))
            if fl:
                return "flight", (fl[0], "")
        tm = parse_time_line(line)
        if tm:
            return "time", tm
        if len(line) <= 40 and not _DEST.search(line):
            d = parse_date_text(line, self.page_date, full_line=True)
            if d:
                return "date", d
        fl = parse_flight_line(line)
        if fl:
            return "flight", fl
        m = _VIA.match(line)
        if m:
            return "via", [f"{x.group('city').strip()} ({x.group('code')})" for x in _DEST.finditer(m.group("rest"))] or [m.group("rest")]
        dests = list(_DEST.finditer(line))
        if dests:
            return "dest", dests
        m = _OPERATED_BY.search(line)
        if m:
            return "operated", m.group("a").strip()
        if _TERMINAL.match(line):
            return "terminal", _TERMINAL.match(line).group("t")
        if _GATE.match(line):
            return "gate", _GATE.match(line).group("g").upper()
        if _CHECKIN.match(line):
            return "checkin", _CHECKIN.match(line).group("c")
        if _BARE_CODE.match(line):
            return "code", _BARE_CODE.match(line).group("code")
        if len(line) <= 40:
            st = canonical_status(line)
            if st:
                return "status", st
        if _BARE_GATE.match(line):
            return "baregate", line
        return "text", line

    @staticmethod
    def _apply_dest(rf: RawFlight, dests: list) -> None:
        first = dests[0]
        if not rf.dest_code:
            rf.dest_city, rf.dest_code = first.group("city").strip(" ,-·"), first.group("code")
            rest = dests[1:]
        else:
            rest = dests
        rf.via.extend(f"{d.group('city').strip(' ,-·')} ({d.group('code')})" for d in rest)

    def _parse_row(self, block: list[str]) -> None:
        """A detected row: normally one physical flight (codeshares included)."""
        kinds = [self._classify(ln) for ln in block]
        n_dest_lines = sum(1 for k, _ in kinds if k in ("dest", "code"))
        if n_dest_lines > 1:
            # Several flights grouped in one container → sequential parsing.
            self._parse_free(block, in_row=True)
            return
        rf = self._new(from_row=True)
        rf.raw = [ln for ln in block if not _ROW_DATE.match(ln)]
        prev_text, prev_kind = None, None
        for (kind, val), line in zip(kinds, block):
            if kind == "time":
                if val["status"]:
                    rf.status = rf.status or val["status"]
                if rf.sched_time is None and not val["explicit_est"]:
                    rf.sched_time, rf.plus_one = val["time"], val["plus_one"]
                elif rf.est_time is None:
                    rf.est_time = val["time"]
            elif kind == "flight":
                nums, airline = val
                rf.flight_numbers += [n for n in nums if n not in rf.flight_numbers]
                if airline and not rf.airline:
                    rf.airline = airline
            elif kind == "dest":
                self._apply_dest(rf, val)
            elif kind == "code" and not rf.dest_code:
                rf.dest_city, rf.dest_code = (prev_text or ""), val
            elif kind == "via":
                rf.via.extend(val)
            elif kind == "operated":
                rf.operating_airline = val
            elif kind == "status":
                rf.status = rf.status or val
            elif kind == "terminal":
                rf.terminal = val
            elif kind in ("gate", "baregate"):
                rf.gate = rf.gate or val
            elif kind == "checkin":
                rf.checkin = val
            elif kind == "date" and not rf.date_from_link:
                rf.explicit_date = val
            elif kind == "rowdate":
                rf.explicit_date, rf.date_from_link = val, True
            elif kind == "text" and prev_kind == "flight" and not rf.airline and len(line) <= 40:
                rf.airline = line  # airline name rendered on its own line
            prev_text = line if kind == "text" else None
            prev_kind = kind
        if rf.sched_time is None and rf.est_time is not None and not rf.status:
            rf.sched_time, rf.est_time = rf.est_time, None
        self._emit(rf)

    def _parse_free(self, lines: list[str], in_row: bool = False) -> None:
        """Sequential parsing (original approach), used outside detected rows."""
        cur: Optional[RawFlight] = None
        prev_text, prev_kind = None, None
        in_codeshare = False
        for line in lines:
            kind, val = self._classify(line)
            if kind == "code" and not in_row:
                kind, val = "text", line  # a lone "CAI"/"SIN" outside a row is too risky (footer links)
            if kind == "codeshare":
                in_codeshare = cur is not None
                continue
            if kind == "rowdate":
                if cur is not None:
                    cur.explicit_date, cur.date_from_link = val, True
                continue
            if kind not in ("flight",):
                in_codeshare = in_codeshare and kind in ("noise",)
            if kind == "date" and not in_row:
                self._emit(cur)
                cur = None
                self.date_label = val
                self.last_time = None
                continue
            if kind == "time":
                if cur is None:
                    cur = self._new(from_row=in_row)
                    cur.sched_time = val["time"]
                    cur.plus_one = val["plus_one"]
                elif val["explicit_est"] or (cur.sched_time and not cur.flight_numbers and not cur.dest_code):
                    cur.est_time = cur.est_time or val["time"]
                elif cur.sched_time is None and not cur.dest_code:
                    cur.sched_time = val["time"]
                else:
                    self._emit(cur)
                    cur = self._new(from_row=in_row)
                    cur.sched_time = val["time"]
                    cur.plus_one = val["plus_one"]
                if val["status"]:
                    cur.status = cur.status or val["status"]
                cur.raw.append(line)
            elif kind == "flight":
                nums, airline = val
                if cur is not None and cur.dest_code and not in_codeshare:
                    inherited = cur.sched_time
                    self._emit(cur)
                    cur = self._new(from_row=in_row)
                    cur.sched_time, cur.time_inherited = inherited, True
                if cur is None:
                    cur = self._new(from_row=in_row)
                cur.flight_numbers += [n for n in nums if n not in cur.flight_numbers]
                cur.airline = cur.airline or airline
                cur.raw.append(line)
            elif cur is None:
                prev_text = line if kind == "text" else None
                prev_kind = kind
                continue
            else:
                cur.raw.append(line)
                if kind == "dest":
                    self._apply_dest(cur, val)
                elif kind == "code" and not cur.dest_code:
                    cur.dest_city, cur.dest_code = (prev_text or ""), val
                elif kind == "via":
                    cur.via.extend(val)
                elif kind == "operated":
                    cur.operating_airline = val
                elif kind == "status":
                    cur.status = cur.status or val
                elif kind == "terminal":
                    cur.terminal = val
                elif kind == "gate":
                    cur.gate = cur.gate or val
                elif kind == "checkin":
                    cur.checkin = val
                elif kind == "text" and prev_kind == "flight" and not cur.airline and len(line) <= 40:
                    cur.airline = line
            prev_text = line if kind == "text" else None
            prev_kind = kind
        self._emit(cur)


def parse_listing_text(text: str, page_date: date) -> ParseResult:
    return ListingParser(page_date).parse(text)


# =============================================================================
# Flight records, date assignment, de-duplication, policies, aggregation
# =============================================================================
@dataclass
class Flight:
    sched_dt: datetime
    flight_number: str
    codeshares: list[str]
    airline: str
    dest_city: str
    dest_code: str
    country: str = "??"
    country_source: str = "unknown"
    via: list[str] = field(default_factory=list)
    status: str = ""
    est_dt: Optional[datetime] = None
    terminal: str = ""
    gate: str = ""
    checkin: str = ""
    date_source: str = ""
    source_page: Optional[date] = None
    order: int = 0
    counted: bool = False
    exclusion_reason: str = ""
    interval_label: str = ""

    @property
    def all_numbers(self) -> list[str]:
        return [self.flight_number] + self.codeshares

    @property
    def is_cancelled(self) -> bool:
        return self.status == "Cancelled"


def assign_dates(raws: list[RawFlight], page_date: date, log: bool = True) -> list[tuple[RawFlight, date, str]]:
    """Give each parsed row a calendar date.

    1. A date carried by the row's own link wins (the live site links each row
       to flight-detail.html?flight_id=ZG54_20261021).
    2. Otherwise explicit date labels in the page text ("Sun, 11 Oct").
    3. Otherwise rows belong to page_date, and a drop of more than 6 hours in
       the (chronologically sorted) scheduled times is treated as midnight; the
       largest same-day segment is anchored to page_date.
    """
    linked = [r for r in raws if r.date_from_link]
    if linked and log and len(linked) < len(raws):
        LOG.warning("%d of %d rows on the %s page had no date in their link; dating those by inference",
                    len(raws) - len(linked), len(raws), page_date)
    out: list[tuple[RawFlight, int, Optional[date], bool]] = []
    labels = {r.explicit_date for r in raws if r.explicit_date and not r.date_from_link}
    segment = 0
    prev_label, prev_minutes = object(), None
    for r in raws:
        if r.date_from_link:
            prev_minutes = r.sched_time.hour * 60 + r.sched_time.minute
            continue
        if r.explicit_date != prev_label:
            prev_label, prev_minutes = r.explicit_date, None
            if r.explicit_date:
                segment = 0
        minutes = r.sched_time.hour * 60 + r.sched_time.minute
        if prev_minutes is not None and minutes < prev_minutes - 360 and not r.explicit_date:
            segment += 1
        prev_minutes = minutes
        out.append((r, segment, r.explicit_date, r.plus_one))

    inferred_rollover = any(seg > 0 for _, seg, _, _ in out)
    shift = 0
    if inferred_rollover and not labels:
        sizes = Counter(seg for _, seg, lab, _ in out)
        anchor = max(sizes, key=lambda k: (sizes[k], -k))
        shift = -anchor
        if log:
            LOG.warning("Listing for %s crosses midnight without date separators; "
                        "segment sizes %s, anchoring segment %d to %s",
                        page_date, dict(sizes), anchor, page_date)
    if log and len(labels) == 1 and page_date not in labels:
        LOG.warning("Page text shows date %s but %s was requested", next(iter(labels)), page_date)

    inferred = {id(r): (seg, lab, plus_one) for r, seg, lab, plus_one in out}
    result = []
    for r in raws:
        if r.date_from_link:
            result.append((r, r.explicit_date, "row-link"))
            continue
        seg, lab, plus_one = inferred[id(r)]
        base = lab or page_date
        offset = seg + (shift if (inferred_rollover and not labels) else 0) + (1 if plus_one else 0)
        src = "label" if lab else "page"
        if seg or plus_one:
            src += "+rollover"
        result.append((r, base + timedelta(days=offset), src))
    return result


def row_link_date(href: str, page_date: date) -> Optional[date]:
    """Date embedded in a row's detail link (YYYYMMDD), if plausible (±3 days)."""
    for m in _LINK_DATE.finditer(href or ""):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if abs((d - page_date).days) <= 3:
            return d
    return None


def build_flights(raws: list[RawFlight], page_date: date, resolver: CountryResolver,
                  keep_date: Optional[date] = None) -> list[Flight]:
    """Convert parsed rows to Flight records; optionally keep only one date."""
    flights = []
    for r, d, src in assign_dates(raws, page_date):
        if keep_date and d != keep_date:
            continue
        sched_dt = datetime.combine(d, r.sched_time, tzinfo=SGT)
        est_dt = None
        if r.est_time:
            est_dt = datetime.combine(d, r.est_time, tzinfo=SGT)
            if est_dt < sched_dt - timedelta(hours=12):
                est_dt += timedelta(days=1)
        country, csrc = resolver.resolve(r.dest_city, r.dest_code)
        flights.append(Flight(
            sched_dt=sched_dt, flight_number=r.flight_numbers[0],
            codeshares=r.flight_numbers[1:],
            airline=r.operating_airline or r.airline, dest_city=r.dest_city,
            dest_code=r.dest_code, country=country, country_source=csrc,
            via=r.via, status=r.status, est_dt=est_dt, terminal=r.terminal,
            gate=r.gate, checkin=r.checkin, date_source=src, source_page=page_date,
            order=r.order,
        ))
    return flights


def _designator(fn: str) -> str:
    return fn[:2]


def dedupe_flights(flights: list[Flight], mode: str = "auto") -> tuple[list[Flight], list[str]]:
    """Merge duplicates and codeshares into one record per physical departure.

    Always merged: records sharing any flight number at the same scheduled time.
    mode 'auto' additionally merges records with the same scheduled time and
    destination when they don't conflict (different carriers, compatible gate /
    check-in row, same cancelled state) — the usual shape of codeshare rows.
    mode 'strict' only merges on shared flight numbers.
    """
    n = len(flights)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_num: dict[tuple, int] = {}
    for i, f in enumerate(flights):
        for num in f.all_numbers:
            key = (num, f.sched_dt)
            if key in by_num:
                union(i, by_num[key])
            else:
                by_num[key] = i

    if mode == "auto":
        groups: dict[tuple, list[int]] = defaultdict(list)
        for i, f in enumerate(flights):
            groups[(f.sched_dt, f.dest_code)].append(i)
        for idxs in groups.values():
            for a_pos, a in enumerate(idxs):
                for b in idxs[a_pos + 1:]:
                    fa, fb = flights[a], flights[b]
                    if _designator(fa.flight_number) == _designator(fb.flight_number):
                        continue  # same carrier twice at one minute ≠ codeshare
                    if fa.is_cancelled != fb.is_cancelled:
                        continue
                    if fa.gate and fb.gate and fa.gate != fb.gate:
                        continue
                    if fa.checkin and fb.checkin and fa.checkin != fb.checkin:
                        continue
                    union(a, b)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    merged, log = [], []
    for root, idxs in sorted(clusters.items()):
        idxs.sort(key=lambda i: flights[i].order)
        base = flights[idxs[0]]
        nums = []
        for i in idxs:
            for num in flights[i].all_numbers:
                if num not in nums:
                    nums.append(num)
        for i in idxs[1:]:
            o = flights[i]
            base.gate = base.gate or o.gate
            base.checkin = base.checkin or o.checkin
            base.status = base.status or o.status
            base.est_dt = base.est_dt or o.est_dt
            base.terminal = base.terminal or o.terminal
        base.flight_number, base.codeshares = nums[0], nums[1:]
        if len(idxs) > 1:
            log.append(f"{base.sched_dt:%Y-%m-%d %H:%M} {base.dest_code}: merged {' + '.join(nums)}")
        merged.append(base)
    merged.sort(key=lambda f: (f.sched_dt, f.flight_number))
    return merged, log


def apply_policies(flights: list[Flight], cfg: Config, intervals: list[Interval]) -> None:
    """Set counted / exclusion_reason / interval_label on each flight."""
    for f in flights:
        basis = f.est_dt if (cfg.time_basis == "estimated" and f.est_dt) else f.sched_dt
        iv = interval_for(basis, intervals)
        f.interval_label = iv.label if iv else ""
        f.counted, f.exclusion_reason = False, ""
        if iv is None:
            f.exclusion_reason = "outside reporting window"
        elif f.dest_code == "SIN":
            f.exclusion_reason = "destination SIN (not a departure)"
        elif f.terminal and f.terminal != cfg.terminal:
            f.exclusion_reason = f"terminal {f.terminal}"
        elif f.is_cancelled and cfg.cancelled_policy == "exclude":
            f.exclusion_reason = "cancelled"
        else:
            f.counted = True


def aggregate(flights: list[Flight], intervals: list[Interval]) -> dict[str, Counter]:
    buckets: dict[str, Counter] = {iv.label: Counter() for iv in intervals}
    counted = [f for f in flights if f.counted]
    for f in counted:
        buckets[f.interval_label][f.country] += 1
    total = sum(sum(c.values()) for c in buckets.values())
    if total != len(counted):  # each flight exactly once
        raise AssertionError(f"Aggregation mismatch: {total} bucketed vs {len(counted)} counted")
    return buckets


# =============================================================================
# Output
# =============================================================================
DETAIL_FIELDS = [
    "report_date", "scheduled_date", "scheduled_time", "time_interval",
    "flight_number", "codeshare_flight_numbers", "airline", "destination_city",
    "destination_iata", "destination_country", "country_source", "via", "status",
    "estimated_time", "terminal", "gate", "check_in", "counted", "exclusion_reason",
    "date_source", "source_page_date",
]


def flight_row(f: Flight, report_date: date, default_terminal: str) -> dict:
    return {
        "report_date": report_date.isoformat(),
        "scheduled_date": f.sched_dt.date().isoformat(),
        "scheduled_time": f.sched_dt.strftime("%H:%M"),
        "time_interval": f.interval_label,
        "flight_number": f.flight_number,
        "codeshare_flight_numbers": " ".join(f.codeshares),
        "airline": f.airline,
        "destination_city": f.dest_city,
        "destination_iata": f.dest_code,
        "destination_country": f.country,
        "country_source": f.country_source,
        "via": "; ".join(f.via),
        "status": f.status,
        "estimated_time": f.est_dt.strftime("%Y-%m-%d %H:%M") if f.est_dt else "",
        "terminal": f.terminal or default_terminal,
        "gate": f.gate,
        "check_in": f.checkin,
        "counted": "yes" if f.counted else "no",
        "exclusion_reason": f.exclusion_reason,
        "date_source": f.date_source,
        "source_page_date": f.source_page.isoformat() if f.source_page else "",
    }


def write_detail_csv(path: Path, flights: list[Flight], cfg: Config) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM: Excel shows "–" correctly
        w = csv.DictWriter(fh, fieldnames=DETAIL_FIELDS)
        w.writeheader()
        for f in flights:
            w.writerow(flight_row(f, cfg.report_date, cfg.terminal))


def write_summary_csv(path: Path, buckets: dict[str, Counter]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Time Interval", "Country Code", "Flights"])
        for label, counts in buckets.items():
            for cc, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                w.writerow([label, cc, n])



# =============================================================================
# Readable report documents (Word / PDF)
# =============================================================================
CHINA_CODE = "CN"  # mainland China; Hong Kong (HK), Macau (MO) and Taiwan (TW) are counted separately


@dataclass
class ReportTable:
    title: str
    date_line: str
    window_line: str
    rows: list[dict]          # label, day, flights, china, countries
    total_flights: int
    total_china: int
    footnotes: list[str]
    status: str


def build_report_table(buckets: dict[str, Counter], intervals: list[Interval], cfg: Config,
                       status: str, notes: list[str], unknown: Counter) -> ReportTable:
    start, end = reporting_window(cfg.report_date)
    rows = []
    for iv in intervals:
        counts = buckets[iv.label]
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        rows.append({
            "label": iv.label,
            "day": f"{iv.start:%a %d %b}" if iv.start.date() != cfg.report_date else "",
            "flights": sum(counts.values()),
            "china": counts.get(CHINA_CODE, 0),
            "countries": "   ".join(f"{cc}\u00a0{n}" for cc, n in ordered) or "–",
        })
    foot = [
        "Times are scheduled departure times, Singapore time (GMT+8). Each physical departure is counted once "
        "(codeshare flight numbers are not counted separately).",
        f"Cancelled flights are {'included' if cfg.cancelled_policy == 'include' else 'not included'}. "
        "China = mainland China (CN); Hong Kong (HK), Macau (MO) and Taiwan (TW) are shown separately.",
    ]
    if unknown:
        foot.append("Destinations with unknown country (shown as ??): "
                    + ", ".join(f"{k} ×{v}" for k, v in unknown.most_common()))
    foot += [f"Note: {n}" for n in notes]
    foot.append(f"Source: changiairport.com, retrieved {datetime.now(SGT):%d %b %Y %H:%M} SGT.")
    return ReportTable(
        title=f"Changi Airport Terminal {cfg.terminal} – Departures by Destination Country",
        date_line=f"{cfg.report_date:%A, %d %B %Y}",
        window_line=f"{start:%H:%M %a %d %b} – {end:%H:%M %a %d %b %Y} (Singapore time)",
        rows=rows,
        total_flights=sum(r["flights"] for r in rows),
        total_china=sum(r["china"] for r in rows),
        footnotes=foot,
        status=status,
    )


def write_report_docx(path: Path, rt: ReportTable) -> None:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)  # A4
    sec.left_margin = sec.right_margin = Cm(1.8)
    sec.top_margin = sec.bottom_margin = Cm(1.5)
    base = doc.styles["Normal"]
    base.font.name, base.font.size = "Calibri", Pt(10.5)

    def para(text, size, bold=False, color=None, space_after=2, align=None):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(space_after)
        if align:
            p.alignment = align
        r = p.add_run(text)
        r.font.size, r.bold = Pt(size), bold
        if color:
            r.font.color.rgb = RGBColor.from_string(color)
        return p

    para(rt.title, 15, bold=True, color="1F2A44")
    para(rt.date_line, 13, bold=True, space_after=0)
    para(rt.window_line, 10, color="555555", space_after=8)
    if not rt.status.startswith("COMPLETE"):
        para(f"DATA INCOMPLETE: {rt.status}", 10.5, bold=True, color="B00020", space_after=6)

    widths = [Cm(3.4), Cm(2.0), Cm(2.4), Cm(9.6)]  # sums to 17.4 cm = text width
    headers = ["Time (SGT)", "Flights", "To China", "Countries (code and number of flights)"]
    table = doc.add_table(rows=1, cols=4)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = False

    def shade(cell, hex_fill):
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_fill)
        tc_pr.append(shd)

    def fill(cells, values, bold=False, fill_hex=None, sub=None):
        from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
        for i, (cell, val) in enumerate(zip(cells, values)):
            cell.width = widths[i]
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            if i in (1, 2):
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(str(val))
            r.bold = bold or i == 0
            if fill_hex == "1F2A44":
                r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            if i == 0 and sub:
                s = p.add_run(f"\n{sub}")
                s.font.size, s.bold = Pt(8.5), False
                s.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            if fill_hex:
                shade(cell, fill_hex)

    fill(table.rows[0].cells, headers, bold=True, fill_hex="1F2A44")
    for i, r in enumerate(rt.rows):
        fill(table.add_row().cells, [r["label"], r["flights"], r["china"], r["countries"]],
             fill_hex="F3F5F9" if i % 2 else None, sub=r["day"] or None)
    fill(table.add_row().cells, ["TOTAL", rt.total_flights, rt.total_china, ""], bold=True, fill_hex="DDE3EE")
    for row in table.rows:  # widths on every cell AND on the grid, fixed layout (Word/LibreOffice/Google Docs)
        for i, cell in enumerate(row.cells):
            cell.width = widths[i]
    for i, col in enumerate(table.columns):
        col.width = widths[i]
    tbl_pr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    margins = OxmlElement("w:tblCellMar")
    for side, val in (("top", 70), ("bottom", 70), ("left", 100), ("right", 100)):
        m = OxmlElement(f"w:{side}")
        m.set(qn("w:w"), str(val))
        m.set(qn("w:type"), "dxa")
        margins.append(m)
    tbl_pr.append(margins)

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    run = p.add_run(f"Total departures: {rt.total_flights}     Of which to China: {rt.total_china}")
    run.bold, run.font.size = True, Pt(12)
    for f in rt.footnotes:
        para(f, 8.5, color="555555", space_after=1)
    doc.save(str(path))


def write_report_pdf(path: Path, rt: ReportTable) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    navy = colors.HexColor("#1F2A44")
    st = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=navy),
        "date": ParagraphStyle("d", fontName="Helvetica-Bold", fontSize=13, leading=17),
        "win": ParagraphStyle("w", fontName="Helvetica", fontSize=10, leading=13, textColor=colors.HexColor("#555555")),
        "warn": ParagraphStyle("x", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=colors.HexColor("#B00020")),
        "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=10, leading=13),
        "cellb": ParagraphStyle("cb", fontName="Helvetica-Bold", fontSize=10, leading=13),
        "head": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=colors.white),
        "total": ParagraphStyle("tt", fontName="Helvetica-Bold", fontSize=12, leading=16),
        "foot": ParagraphStyle("f", fontName="Helvetica", fontSize=8.5, leading=11, textColor=colors.HexColor("#555555")),
    }
    story = [Paragraph(escape(rt.title), st["title"]), Spacer(1, 4),
             Paragraph(escape(rt.date_line), st["date"]), Paragraph(escape(rt.window_line), st["win"]), Spacer(1, 10)]
    if not rt.status.startswith("COMPLETE"):
        story += [Paragraph(escape(f"DATA INCOMPLETE: {rt.status}"), st["warn"]), Spacer(1, 6)]

    data = [[Paragraph(h, st["head"]) for h in
             ["Time (SGT)", "Flights", "To China", "Countries (code and number of flights)"]]]
    for r in rt.rows:
        label = f"<b>{escape(r['label'])}</b>"
        if r["day"]:
            label += f"<br/><font size=8.5 color='#666666'>{escape(r['day'])}</font>"
        data.append([Paragraph(label, st["cell"]), str(r["flights"]), str(r["china"]),
                     Paragraph(escape(r["countries"]).replace("\u00a0", "&nbsp;").replace("   ", "&nbsp;&nbsp;&nbsp; "), st["cell"])])
    data.append([Paragraph("TOTAL", st["cellb"]), str(rt.total_flights), str(rt.total_china), ""])

    table = Table(data, colWidths=[3.4 * cm, 2.0 * cm, 2.4 * cm, 9.6 * cm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C0CC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (2, -1), "CENTER"),
        ("FONTNAME", (1, 1), (2, -2), "Helvetica"),
        ("FONTSIZE", (1, 1), (2, -1), 10.5),
        ("FONTNAME", (1, -1), (2, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DDE3EE")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, len(data) - 1):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F3F5F9")))
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 12),
              Paragraph(f"Total departures: {rt.total_flights}&nbsp;&nbsp;&nbsp;&nbsp; "
                        f"Of which to China: {rt.total_china}", st["total"]), Spacer(1, 6)]
    story += [Paragraph(escape(f), st["foot"]) for f in rt.footnotes]

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                      topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                      title=rt.title, author="changi_scraper").build(story)


def write_report_documents(cfg: Config, rt: ReportTable, suffix: str) -> list[Path]:
    """Write the Word and/or PDF report. A missing library is logged, not fatal."""
    out = []
    wanted = {"both": ("docx", "pdf"), "docx": ("docx",), "pdf": ("pdf",)}.get(cfg.doc_format, ())
    for fmt in wanted:
        path = cfg.output_dir / f"{cfg.file_prefix}_report_{cfg.report_date}{suffix}.{fmt}"
        try:
            (write_report_docx if fmt == "docx" else write_report_pdf)(path, rt)
            out.append(path)
        except ImportError as exc:
            LOG.warning("Skipping %s report (%s). Install it with: pip install -r requirements.txt", fmt, exc)
        except PermissionError:
            LOG.error("Could not write %s – is it open in Word/a PDF viewer? Close it and re-run.", path)
    return out


def format_report(buckets: dict[str, Counter], intervals: list[Interval], flights: list[Flight],
                  cfg: Config, status: str, notes: list[str], unknown: Counter) -> str:
    start, end = reporting_window(cfg.report_date)
    bar = "=" * 66
    lines = [
        bar,
        f"Changi Airport Terminal {cfg.terminal} — departures by destination country",
        f"Report date : {cfg.report_date:%a %d %b %Y}",
        f"Window      : {start:%d %b %H:%M} → {end:%d %b %H:%M} (Singapore time, end exclusive)",
        f"Counting    : {cfg.time_basis} time; cancelled {cfg.cancelled_policy}d; "
        f"codeshares merged ({cfg.codeshare_mode})",
        f"Data status : {status}",
        bar,
    ]
    for iv in intervals:
        counts = buckets[iv.label]
        total = sum(counts.values())
        lines.append(f"\n{iv.label}  ({iv.start:%a %d %b})  —  {total} flight(s), "
                     f"{counts.get(CHINA_CODE, 0)} to China")
        if not counts:
            lines.append("    (none)")
        for cc, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {cc}: {n}")
    grand = sum(sum(c.values()) for c in buckets.values())
    lines.append("\n" + "-" * 66)
    lines.append(f"TOTAL {start:%d %b %H:%M} – {end:%d %b %H:%M}: {grand} flight(s)")
    excluded = Counter(f.exclusion_reason for f in flights
                       if not f.counted and f.exclusion_reason != "outside reporting window")
    if excluded:
        lines.append("Excluded in window: " + ", ".join(f"{n} {r}" for r, n in excluded.items()))
    if unknown:
        lines.append("Unknown destinations (counted as '??'): "
                     + ", ".join(f"{k} ×{v}" for k, v in unknown.most_common()))
    for note in notes:
        lines.append(f"NOTE: {note}")
    return "\n".join(lines)


# =============================================================================
# Browser automation
# =============================================================================
# One JavaScript helper library, injected per call. It walks open shadow roots,
# identifies elements by visible text/ARIA/geometry, and never relies on the
# site's CSS class names.
JS_LIB = r"""
var CT = (function(){
  function norm(s){ return (s||'').replace(/\s+/g,' ').trim(); }
  function parentOf(el){ return el.parentElement || (el.parentNode && el.parentNode.host) || null; }
  function all(root){
    var out=[], stack=[root||document.documentElement];
    while(stack.length){
      var n=stack.pop();
      if(n.nodeType===1) out.push(n);
      if(n.shadowRoot) stack.push(n.shadowRoot);
      var kids=n.children||[];
      for(var i=kids.length-1;i>=0;i--) stack.push(kids[i]);
    }
    return out;
  }
  function visible(el){
    if(!el.getBoundingClientRect) return false;
    var r=el.getBoundingClientRect(); if(r.width<1||r.height<1) return false;
    if(el.checkVisibility) return el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    var cs=getComputedStyle(el); return cs.visibility!=='hidden' && cs.display!=='none';
  }
  function leafText(el){
    if(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].indexOf(el.tagName)>=0) return null;
    for(var i=0;i<el.children.length;i++){ if(norm(el.children[i].textContent)) return null; }
    if(el.shadowRoot && norm(el.shadowRoot.textContent)) return null;
    var t=norm(el.textContent); return t||null;
  }
  function clsOf(el){ var c=el.className; return typeof c==='string'?c:(c&&c.baseVal)||''; }
  function navHref(el){
    var n=el, d=0;
    while(n && d<25){
      if(n.tagName==='A' && n.getAttribute('href')){
        var h=n.getAttribute('href').trim();
        if(h && h!=='#' && h.indexOf('javascript:')!==0){
          try{ var u=new URL(h, location.href);
               if(u.href.split('#')[0]!==location.href.split('#')[0]) return u.href; }
          catch(e){ return h; }
        }
      }
      n=parentOf(n); d++;
    }
    return null;
  }
  function htmlTarget(el){ var n=el; while(n && (n instanceof SVGElement)) n=parentOf(n); return n||el; }
  function clickable(el){
    var n=htmlTarget(el), d=0;
    while(n && d<4){
      var role=n.getAttribute&&n.getAttribute('role');
      if(n.tagName==='BUTTON'||n.tagName==='A'||role==='button'||role==='gridcell'||role==='option'||
         (n.hasAttribute&&n.hasAttribute('tabindex'))) return n;
      n=parentOf(n); d++;
    }
    return htmlTarget(el);
  }
  function rect(el){ var r=el.getBoundingClientRect();
    return {x:r.left,y:r.top,w:r.width,h:r.height,cx:r.left+r.width/2,cy:r.top+r.height/2}; }
  function describe(el){ if(!el) return null;
    return {tag:el.tagName.toLowerCase(), id:el.id||'', cls:clsOf(el).slice(0,120),
            role:el.getAttribute('role')||'', aria:el.getAttribute('aria-label')||'',
            text:norm(el.textContent).slice(0,80), inShadow: el.getRootNode()!==document,
            rect: rect(el)}; }

  var DEST=/\([A-Z]{3}\)/, TIME=/(^|[^\d])([01]\d|2[0-3]):[0-5]\d([^\d]|$)/,
      FLT=/(^|[^A-Za-z0-9])([A-Z]{2}|[A-Z]\d|\d[A-Z]) ?\d{1,4}/,
      MONTH_HDR=/^(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)\.?,?\s+(\d{4})$/i,
      DATE_TXT=/\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(,?\s+\d{4})?\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(,?\s+\d{4})?\b|\b\d{1,2}\/\d{1,2}\/\d{4}\b/i,
      MON={jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12};

  function rowTexts(){
    var rows=[], seen=new Set();
    var leaves=all().filter(function(e){ var t=leafText(e); return t && t.length<90 && DEST.test(t) && visible(e); });
    leaves.forEach(function(lf){
      var n=lf, d=0, found=null;
      while(n && d<12){
        var tx=n.innerText||n.textContent||'';
        if(TIME.test(tx) && FLT.test(tx)){ found=n; break; }
        n=parentOf(n); d++;
      }
      if(!found || seen.has(found)) return;
      var nd=((found.innerText||found.textContent||'').match(/\([A-Z]{3}\)/g)||[]).length;
      if(nd>6) return;
      seen.add(found); rows.push(found);
    });
    rows=rows.filter(function(r){ return !rows.some(function(o){ return o!==r && r.contains(o); }); });
    return rows.map(function(r){
      var a = r.closest ? r.closest('a[href]') : null;
      if(!a && r.querySelector) a = r.querySelector('a[href]');
      return {text: r.innerText||r.textContent||'', el:r, href: a ? a.getAttribute('href') : ''};
    });
  }
  function dayLeaves(scope){
    return all(scope).filter(function(e){
      var t=leafText(e); if(!t || !/^\d{1,2}$/.test(t)) return false;
      var v=parseInt(t,10); return v>=1 && v<=31 && visible(e);
    });
  }
  function calendarPanels(){
    var out=[];
    all().forEach(function(e){
      if(!visible(e)) return; var t=leafText(e); if(!t) return;
      var m=t.match(MONTH_HDR); if(!m || navHref(e)) return;
      var n=parentOf(e), d=0, panel=null;
      while(n && d<8){ if(dayLeaves(n).length>=28){ panel=n; break; } n=parentOf(n); d++; }
      if(!panel) return;
      out.push({header:e, panel:panel, month:MON[m[1].slice(0,3).toLowerCase()], year:parseInt(m[m.length-1],10), text:t});
    });
    return out;
  }
  function isDisabled(el){
    var n=el, d=0;
    while(n && d<4){
      if(n.hasAttribute && (n.hasAttribute('disabled') || n.getAttribute('aria-disabled')==='true')) return true;
      if(/disabled|unavailable|blocked|inactive/i.test(clsOf(n))) return true;
      n=parentOf(n); d++;
    }
    return false;
  }
  function isSelected(el){
    var n=el, d=0;
    while(n && d<4){
      if(n.getAttribute && (n.getAttribute('aria-selected')==='true' || n.getAttribute('aria-current')==='date' ||
         n.getAttribute('aria-pressed')==='true')) return true;
      if(/(^|[\s_-])(selected|active|current|chosen)([\s_-]|$)/i.test(clsOf(n))) return true;
      n=parentOf(n); d++;
    }
    return false;
  }
  function pickDay(panel, day, dim){
    var cells=dayLeaves(panel), vals=cells.map(function(c){ return parseInt(leafText(c),10); });
    for(var i=0;i+dim<=vals.length;i++){
      if(vals[i]!==1) continue;
      var ok=true; for(var k=1;k<dim;k++){ if(vals[i+k]!==k+1){ ok=false; break; } }
      if(ok){ var c=cells[i+day-1]; return {el:c, how:'contiguous-run', disabled:isDisabled(c), selected:isSelected(c), n:cells.length}; }
    }
    var same=cells.filter(function(c,i){ return vals[i]===day; });
    if(!same.length) return {el:null, how:'not-found', n:cells.length};
    var c2 = same.length===1 ? same[0] : (day<=14 ? same[0] : same[same.length-1]);
    return {el:c2, how:'value-match('+same.length+')', disabled:isDisabled(c2), selected:isSelected(c2), n:cells.length};
  }
  function arrows(panel, header){
    var hr=rect(header), scope=panel;
    for(var lvl=0; lvl<4 && scope; lvl++, scope=parentOf(scope)){
      var days=dayLeaves(scope), cands=[], seen=new Set();
      all(scope).forEach(function(e){
        if(e===header || !visible(e) || days.indexOf(e)>=0) return;
        if(e.contains(header)) return;
        for(var i=0;i<days.length;i++){ if(e.contains(days[i])) return; }
        var tag=e.tagName.toUpperCase(), role=e.getAttribute('role');
        var lbl=norm((e.getAttribute('aria-label')||'')+' '+(e.getAttribute('title')||''));
        var lt=leafText(e), cls=clsOf(e);
        var arrowChar = lt && /^[<>‹›«»←→❮❯〈〉＜＞]$/.test(lt);
        var icon = tag==='SVG'||tag==='IMG'||tag==='I'||(tag==='SPAN'&&!norm(e.textContent));
        var btn = tag==='BUTTON'||role==='button'||(e.hasAttribute('tabindex')&&!norm(e.textContent));
        var labelled = /next|prev|previous|forward|back|left|right|month/i.test(lbl) || /next|prev|arrow|chevron|left|right/i.test(cls);
        if(!(arrowChar||icon||btn||labelled)) return;
        var tgt=clickable(e); if(seen.has(tgt) || tgt.contains(header)) return;
        for(var j=0;j<days.length;j++){ if(tgt.contains(days[j])) return; }
        var r=rect(tgt); if(r.w>120||r.h>120||r.w<4) return;
        if(Math.abs(r.cy-hr.cy) > Math.max(40, hr.h*2)) return;
        seen.add(tgt);
        var tl=norm((tgt.getAttribute('aria-label')||'')+' '+(tgt.getAttribute('title')||'')+' '+lbl+' '+cls+' '+clsOf(tgt)+' '+(lt||''));
        cands.push({el:tgt, r:r, lbl:tl});
      });
      if(!cands.length) continue;
      var next=cands.find(function(c){ return /next|forward|right|›|»|→|❯|>/i.test(c.lbl); });
      var prev=cands.find(function(c){ return /prev|back|left|‹|«|←|❮|</i.test(c.lbl); });
      if(next||prev) return {next:next?next.el:null, prev:prev?prev.el:null, how:'label', n:cands.length};
      cands.sort(function(a,b){ return a.r.cx-b.r.cx; });
      if(cands.length>=2) return {prev:cands[0].el, next:cands[cands.length-1].el, how:'position', n:cands.length};
      var only=cands[0];
      return only.r.cx>hr.cx ? {next:only.el, prev:null, how:'single-right', n:1} : {prev:only.el, next:null, how:'single-left', n:1};
    }
    return {next:null, prev:null, how:'none', n:0};
  }
  function depthOf(n){ var d=0; while(n){ d++; n=parentOf(n); } return d; }
  function sharedDepth(a,b){
    if(!a||!b) return 0; var anc=new Set(), n=a;
    while(n){ anc.add(n); n=parentOf(n); }
    n=b; while(n && !anc.has(n)) n=parentOf(n);
    return n? depthOf(n):0;
  }
  function dateLabelNear(el){
    var n=el, d=0;
    while(n && d<5){
      var lbls = n.querySelectorAll ? n.querySelectorAll('label') : [];
      for(var i=0;i<lbls.length;i++){ if(/date/i.test(norm(lbls[i].textContent))) return true; }
      n=parentOf(n); d++;
    }
    return false;
  }
  function triggers(){
    var out=[], seen=new Set();
    var rws=rowTexts(), firstRow = rws.length ? rws[0].el : null;
    all().forEach(function(e){
      if(!visible(e)) return;
      var tag=e.tagName, score=0, why=[];
      if(tag==='INPUT' && e.type==='date'){ score+=10; why.push('input[type=date]'); }
      var lbl=norm([e.getAttribute('aria-label'),e.getAttribute('title'),e.getAttribute('placeholder'),e.getAttribute('name')].filter(Boolean).join(' '));
      if(/date|calendar/i.test(lbl)){ score+=3; why.push('label'); }
      var hp=e.getAttribute('aria-haspopup'); if(hp && hp!=='false' && hp!=='menu'){ score+=1; why.push('haspopup'); }
      var lt=leafText(e);
      if(lt && lt.length<=40 && DATE_TXT.test(lt) && !/\d{1,2}:\d{2}/.test(lt)){ score+=3; why.push('date-text'); }
      if(tag==='INPUT' && e.value && DATE_TXT.test(e.value)){ score+=3; why.push('input-value'); }
      if(score<3) return;
      if(navHref(e)) return;
      var n=e, d=0;
      while(n && d<5){ var tc=n.textContent||''; if(DEST.test(tc) && tc.length<400) return; n=parentOf(n); d++; }
      var r=rect(e); if(r.h>150||r.w>700) return;
      var tgt = tag==='INPUT' ? e : clickable(e);
      if(seen.has(tgt)) return; seen.add(tgt);
      var isBtn = tgt.tagName==='BUTTON'||tgt.getAttribute('role')==='button';
      if(dateLabelNear(tgt)){ score+=2; why.push('date-label'); }
      out.push({el:tgt, score:score+(isBtn?1:0), why:why.join(','), text: lt||e.value||lbl,
                isInput: tag==='INPUT', inputType: e.type||'', near: sharedDepth(tgt, firstRow), desc: describe(tgt)});
    });
    // The control that shares the deepest ancestor with the flight rows belongs to
    // the listing (the live site also has a site-wide search date box in the header).
    var maxNear = Math.max.apply(null, out.map(function(o){ return o.near; }).concat([0]));
    out.forEach(function(o){ if(maxNear>0 && o.near===maxNear){ o.score+=3; o.why+=',near-listing'; } });
    out.sort(function(a,b){ return (b.score-a.score) || (b.near-a.near); });
    return out.slice(0,8);
  }
  function showMore(word){
    var want='SHOW '+word+' FLIGHTS', best=null;
    all().forEach(function(e){
      if(!visible(e)) return; var t=norm(e.textContent).toUpperCase();
      if(t.indexOf(want)<0 || t.length>want.length+20) return; best=e;
    });
    if(!best) return null;
    var h=navHref(best); if(h) return {el:null, href:h};
    var tgt=clickable(best);
    if(tgt.disabled || tgt.getAttribute('aria-disabled')==='true') return null;
    return {el:tgt};
  }
  function covered(el){
    var r=el.getBoundingClientRect(), x=r.left+r.width/2, y=r.top+r.height/2;
    var root=el.getRootNode(); var hit=(root.elementFromPoint?root:document).elementFromPoint(x,y);
    if(!hit) return true; return !(hit===el || el.contains(hit) || hit.contains(el));
  }
  function report(){
    var hosts=all().filter(function(e){ return !!e.shadowRoot; });
    var frames=Array.prototype.map.call(document.querySelectorAll('iframe'), function(f){ return {src:f.src, title:f.title}; });
    var cps=calendarPanels().map(function(p){ return {text:p.text, month:p.month, year:p.year,
      header:describe(p.header), panel:describe(p.panel), days:dayLeaves(p.panel).length}; });
    var trg=triggers().map(function(t){ return {score:t.score, why:t.why, near:t.near, text:t.text, desc:t.desc}; });
    var rws=rowTexts();
    var later=showMore('LATER'), earlier=showMore('EARLIER');
    return {url:location.href, title:document.title, shadowHosts:hosts.length,
            shadowHostTags:hosts.slice(0,20).map(function(h){ return h.tagName.toLowerCase(); }),
            iframes:frames, calendarPanels:cps, dateTriggers:trg, rowCount:rws.length,
            sampleRows:rws.slice(0,5).map(function(r){ return r.text; }),
            sampleRowHrefs:rws.slice(0,5).map(function(r){ return r.href; }),
            sampleRowElement: rws.length? describe(rws[0].el):null,
            showLater: later? (later.el? describe(later.el): {href:later.href}) : null,
            showEarlier: earlier? (earlier.el? describe(earlier.el): {href:earlier.href}) : null};
  }
  return {norm:norm, all:all, visible:visible, leafText:leafText, rowTexts:rowTexts,
          calendarPanels:calendarPanels, dayLeaves:dayLeaves, pickDay:pickDay, arrows:arrows,
          triggers:triggers, showMore:showMore, covered:covered, report:report,
          isSelected:isSelected, describe:describe, navHref:navHref};
})();
"""

JS_DISPATCH_CLICK = r"""
var el=arguments[0]; var r=el.getBoundingClientRect();
var o={bubbles:true,cancelable:true,composed:true,view:window,clientX:r.left+r.width/2,clientY:r.top+r.height/2,button:0};
['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t){
  var E=(t.indexOf('pointer')===0 && window.PointerEvent)?PointerEvent:MouseEvent; el.dispatchEvent(new E(t,o)); });
"""

JS_SET_NATIVE_DATE = r"""
var el=arguments[0], v=arguments[1];
var setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
setter.call(el, v);
el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));
"""

JS_COOKIE = r"""
var words=/^(accept|accept all|accept all cookies|i agree|agree|got it|ok|okay|allow all|continue)$/i, hit=null;
function aboutCookies(e){ var n=e, d=0; while(n && d<6){ if(/cookie/i.test(n.textContent||'') && (n.textContent||'').length<3000) return true; n=n.parentElement; d++; } return false; }
CT.all().forEach(function(e){ if(hit) return; var t=CT.leafText(e);
  if(t && words.test(t) && CT.visible(e) && !CT.navHref(e) && aboutCookies(e)){ var b=e.closest&&e.closest('button,[role=button]'); hit=b||e; } });
if(hit){ hit.click(); return CT.norm(hit.textContent); } return null;
"""


def js(driver, body: str, *args):
    return driver.execute_script(JS_LIB + "\n" + body, *args)


class DebugRecorder:
    """Saves screenshots, HTML, visible text and a DOM report when things go wrong."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def dump(self, driver, label: str) -> Path:
        stamp = datetime.now().strftime("%H%M%S")
        base = self.folder / f"{stamp}_{re.sub(r'[^A-Za-z0-9_-]+', '_', label)}"
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        for suffix, fn in (
            (".png", lambda p: driver.save_screenshot(str(p))),
            (".html", lambda p: p.write_text(driver.page_source, encoding="utf-8")),
            (".txt", lambda p: p.write_text(driver.execute_script("return document.body.innerText") or "", encoding="utf-8")),
            ("_dom_report.json", lambda p: p.write_text(json.dumps(dom_report(driver), indent=2, default=str), encoding="utf-8")),
        ):
            try:
                fn(base.with_name(base.name + suffix))
            except Exception as exc:
                LOG.debug("debug dump %s failed: %s", suffix, exc)
        (LOG.info if label.startswith("inspect") else LOG.error)("Debug artefacts saved: %s.*", base)
        return base


def dom_report(driver) -> dict:
    rep = js(driver, "return CT.report();")
    rep["frames_checked"] = []
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for i in range(len(frames)):
            try:
                driver.switch_to.frame(i)
                sub = js(driver, "return {cal: CT.calendarPanels().length, trg: CT.triggers().length, url: location.href};")
                rep["frames_checked"].append({"index": i, **sub})
            except Exception as exc:
                rep["frames_checked"].append({"index": i, "error": str(exc)[:200]})
            finally:
                driver.switch_to.default_content()
    except Exception:
        pass
    return rep


class NavigationGuard:
    """Detects (and recovers from) leaving the departures listing."""

    def __init__(self, driver, expected_url: str):
        self.driver = driver
        self.expected = urlparse(expected_url)
        self.handle = driver.current_window_handle

    def on_listing(self) -> bool:
        cur = urlparse(self.driver.current_url)
        if self.expected.scheme == "file":
            return cur.path == self.expected.path
        return cur.netloc == self.expected.netloc and cur.path.rstrip("/") == self.expected.path.rstrip("/")

    def check(self, context: str) -> None:
        d = self.driver
        extra = [h for h in d.window_handles if h != self.handle]
        if extra:
            LOG.warning("%s opened %d new tab(s); closing them", context, len(extra))
            for h in extra:
                d.switch_to.window(h)
                d.close()
            d.switch_to.window(self.handle)
        if not self.on_listing():
            bad = d.current_url
            LOG.error("%s navigated away from the departures listing to %s", context, bad)
            try:
                d.back()
                WebDriverWait(d, 15).until(lambda _: self.on_listing())
            except Exception:
                pass
            raise PageNavigationError(f"{context} left the departures page (went to {bad})")
        q = parse_qs(self.expected.query)
        if "terminal" in q:
            cur_q = parse_qs(urlparse(d.current_url).query)
            if cur_q.get("terminal") and cur_q["terminal"] != q["terminal"]:
                raise PageNavigationError(f"Terminal changed to {cur_q['terminal']} after {context}")


def _start_browser(cfg: Config, kind: str):
    if kind == "edge":
        from selenium.webdriver.edge.options import Options as EdgeOptions
        from selenium.webdriver.edge.service import Service as EdgeService
        opts = EdgeOptions()
    else:
        opts = Options()
    if cfg.headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,2400")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--lang=en-SG")
    opts.add_argument("--disable-notifications")
    if sys.platform.startswith("linux"):
        opts.add_argument("--no-sandbox")
    if kind == "edge":
        return webdriver.Edge(service=EdgeService(), options=opts)  # Selenium Manager finds msedgedriver
    if cfg.chrome_binary:
        opts.binary_location = cfg.chrome_binary
    service = Service(executable_path=cfg.chromedriver) if cfg.chromedriver else Service()
    return webdriver.Chrome(service=service, options=opts)  # Selenium Manager finds chromedriver


def build_driver(cfg: Config):
    """Chrome by default; with browser='auto', fall back to Microsoft Edge (common on
    managed work PCs). Both are Chromium-based, so everything else is identical."""
    if webdriver is None:
        raise ScraperError("selenium is not installed: pip install -r requirements.txt")
    order = {"chrome": ["chrome"], "edge": ["edge"]}.get(cfg.browser, ["chrome", "edge"])
    errors = []
    driver = None
    for kind in order:
        try:
            driver = _start_browser(cfg, kind)
            LOG.info("Using %s", "Microsoft Edge" if kind == "edge" else "Google Chrome")
            break
        except Exception as exc:
            msg = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
            errors.append(f"{kind}: {msg}")
            LOG.warning("Could not start %s (%s)", kind, msg)
    if driver is None:
        raise ScraperError("Could not start a browser. Install Google Chrome or Microsoft Edge. Details: "
                           + " | ".join(errors))
    driver.set_page_load_timeout(cfg.page_timeout * 2)
    try:  # make "today" on the site match Singapore, and hide the headless UA marker
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Singapore"})
        ua = driver.execute_script("return navigator.userAgent")
        if "Headless" in ua:
            driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
    except Exception as exc:  # pragma: no cover
        LOG.debug("CDP tweaks skipped: %s", exc)
    return driver


def safe_click(driver, guard: NavigationGuard, el, context: str) -> None:
    """Real mouse click when the element is unobstructed, synthetic events otherwise."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", el)
    time.sleep(0.2)
    try:
        if js(driver, "return CT.covered(arguments[0]);", el):
            raise WebDriverException("element covered")
        ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
    except WebDriverException as exc:
        LOG.debug("%s: falling back to dispatched click (%s)", context, str(exc).splitlines()[0][:80])
        driver.execute_script(JS_DISPATCH_CLICK, el)
    time.sleep(0.3)
    guard.check(context)


@dataclass
class Snapshot:
    text: str
    rows: int
    first_row: object
    flights: list[RawFlight]
    unparsed: list[str]


def annotate_text(body_text: str, row_texts: list[str],
                  row_dates: Optional[list[Optional[date]]] = None) -> tuple[str, int]:
    """Insert ROW markers around each detected row inside the page text, so the
    parser knows row boundaries but still sees date separators between rows.
    row_dates (from each row's link) are added as <<<DATE:...>>> lines."""
    body = [ln.strip() for ln in (body_text or "").splitlines() if ln.strip()]
    row_dates = row_dates or [None] * len(row_texts)
    pairs = [([ln.strip() for ln in t.splitlines() if ln.strip()], d) for t, d in zip(row_texts, row_dates)]
    pairs = [(r, d) for r, d in pairs if r]
    rows = [r for r, _ in pairs]
    tail = {i: ([f"<<<DATE:{d.isoformat()}>>>"] if d else []) for i, (_, d) in enumerate(pairs)}
    out, cursor, located, marks, mark_row = [], 0, 0, {}, {}
    for ri, r in enumerate(rows):
        L = len(r)
        for start in range(cursor, len(body) - L + 1):
            if body[start] == r[0] and body[start:start + L] == r:
                marks[start] = start + L
                mark_row[start] = ri
                cursor = start + L
                located += 1
                break
    if rows and located < len(rows) / 2:
        # Rows not in body.innerText (e.g. rendered in shadow DOM): rows only.
        LOG.debug("Only %d/%d rows located in page text; using row texts alone", located, len(rows))
        return "\n".join(f"{ROW_START}\n" + "\n".join(r + tail[i]) + f"\n{ROW_END}"
                         for i, r in enumerate(rows)), len(rows)
    i = 0
    while i < len(body):
        if i in marks:
            out.append(ROW_START)
            out.extend(body[i:marks[i]])
            out.extend(tail[mark_row[i]])
            out.append(ROW_END)
            i = marks[i]
        else:
            out.append(body[i])
            i += 1
    return "\n".join(out), located


def snapshot(driver, page_date: date) -> Snapshot:
    data = js(driver, "return {body: document.body.innerText, rows: CT.rowTexts()};")
    rows = data["rows"] or []
    text, _ = annotate_text(data["body"] or "", [r["text"] for r in rows],
                            [row_link_date(r.get("href") or "", page_date) for r in rows])
    res = parse_listing_text(text, page_date)
    return Snapshot(text, len(rows), rows[0]["el"] if rows else None, res.flights, res.unparsed_rows)


def snapshot_keys(snap: Snapshot) -> set:
    return {(f.sched_time, tuple(f.flight_numbers), f.dest_code) for f in snap.flights}


def fingerprint(snap: Snapshot) -> str:
    return "|".join(f"{f.sched_time}{','.join(f.flight_numbers)}{f.status}{f.est_time}" for f in snap.flights)


def wait_for_listing(driver, timeout: int) -> None:
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.7).until(
            lambda d: js(d, "return CT.rowTexts().length;") > 0)
    except TimeoutException:
        raise ListingLoadError(f"No flight rows appeared within {timeout}s")


def wait_for_stable(driver, page_date: date, timeout: float = 30, polls: int = 3) -> Snapshot:
    """Wait until the parsed listing stops changing for `polls` consecutive checks."""
    deadline, last, same = time.time() + timeout, None, 0
    snap = snapshot(driver, page_date)
    while time.time() < deadline:
        fp = fingerprint(snap)
        same = same + 1 if (fp == last and snap.rows > 0) else 0
        if same >= polls - 1:
            return snap
        last = fp
        time.sleep(0.6)
        snap = snapshot(driver, page_date)
    LOG.warning("Listing did not settle within %ss; continuing with latest snapshot", timeout)
    return snap


def open_departures(driver, cfg: Config) -> NavigationGuard:
    LOG.info("Opening %s", cfg.site_url)
    driver.get(cfg.site_url)
    WebDriverWait(driver, cfg.page_timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete")
    landed = driver.current_url  # after any redirect
    if "departure" not in urlparse(landed).path.lower():
        raise PageNavigationError(f"Opening {cfg.site_url} landed on {landed}, not a departures page")
    guard = NavigationGuard(driver, landed)
    try:
        clicked = js(driver, JS_COOKIE)
        if clicked:
            LOG.info("Dismissed banner via '%s'", clicked)
            time.sleep(0.5)
    except Exception:
        pass
    wait_for_listing(driver, cfg.page_timeout)
    guard.check("initial load")
    body = (driver.execute_script("return document.body.innerText") or "").lower()
    if "departure" not in body:
        LOG.warning("The word 'departure' does not appear on the page; verify this is the departures listing")
    return guard


# ---- Calendar ---------------------------------------------------------------
@contextmanager
def in_frame(driver, frame_index: Optional[int]):
    if frame_index is None:
        yield
        return
    driver.switch_to.frame(frame_index)
    try:
        yield
    finally:
        driver.switch_to.default_content()


def find_triggers(driver) -> tuple[Optional[int], list]:
    """Date-picker trigger candidates in the main document, else in iframes."""
    trg = js(driver, "return CT.triggers();")
    if trg:
        return None, trg
    for i in range(len(driver.find_elements(By.TAG_NAME, "iframe"))):
        try:
            with in_frame(driver, i):
                trg = js(driver, "return CT.triggers();")
            if trg:
                LOG.info("Date picker trigger found inside iframe #%d", i)
                return i, trg
        except (NoSuchFrameException, WebDriverException):
            continue
    return None, []


def read_displayed_date(driver, ref: date) -> Optional[date]:
    """Date shown in the listing's own date control (the best-ranked trigger only;
    other date boxes on the page, e.g. a header search widget, are ignored)."""
    frame, trg = find_triggers(driver)
    if not trg:
        return None
    return parse_date_text(trg[0].get("text") or "", ref)


def date_evidence(driver, target: date) -> dict:
    """Independent signals for which date the listing shows."""
    ev = {"shown": read_displayed_date(driver, target), "url": None, "rows": Counter()}
    q = parse_qs(urlparse(driver.current_url).query)
    for key in ("date", "d", "day"):
        if q.get(key):
            try:
                ev["url"] = date.fromisoformat(q[key][0][:10])
            except ValueError:
                pass
            break
    try:
        rows = js(driver, "return CT.rowTexts().map(function(r){ return r.href; });") or []
        for href in rows:
            d = row_link_date(href or "", target)
            if d:
                ev["rows"][d] += 1
    except WebDriverException:
        pass
    return ev


def date_confirmed(ev: dict, target: date) -> tuple[bool, str]:
    """Displayed date must match; if unreadable, URL or row links must agree.
    Row links must not show a different day for most rows."""
    rows = ev["rows"]
    if rows:
        top, n = rows.most_common(1)[0]
        if n > rows.get(target, 0) and top != target:
            return False, f"most rows are dated {top}"
    if ev["shown"] is not None:
        return ev["shown"] == target, f"date box shows {ev['shown']}"
    if ev["url"] == target or rows.get(target):
        return True, "URL/row links (date box unreadable)"
    return False, "date unreadable"


def calendar_panels(driver) -> list:
    return js(driver, "return CT.calendarPanels();") or []


def open_calendar(driver, guard: NavigationGuard, frame: Optional[int], triggers: list) -> list:
    for t in triggers:
        if t.get("isInput") and t.get("inputType") == "date":
            continue
        LOG.debug("Trying date trigger (score %s, %s): %r", t["score"], t["why"], t.get("text"))
        with in_frame(driver, frame):
            if calendar_panels(driver):
                return calendar_panels(driver)
            el = t["el"]
        with in_frame(driver, frame):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                if js(driver, "return CT.covered(arguments[0]);", el):
                    driver.execute_script(JS_DISPATCH_CLICK, el)
                else:
                    ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
            except StaleElementReferenceException:
                continue
            except WebDriverException:
                driver.execute_script(JS_DISPATCH_CLICK, el)
        guard.check("opening date picker")
        try:
            with in_frame(driver, frame):
                return WebDriverWait(driver, 6, poll_frequency=0.4).until(lambda d: calendar_panels(d) or False)
        except TimeoutException:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            continue
    return []


def navigate_to_month(driver, guard: NavigationGuard, frame: Optional[int], target: date) -> dict:
    for _ in range(26):
        with in_frame(driver, frame):
            panels = calendar_panels(driver)
        if not panels:
            raise CalendarError("Calendar closed unexpectedly while changing month")
        for p in panels:
            if (p["year"], p["month"]) == (target.year, target.month):
                return p
        p = min(panels, key=lambda p: (p["year"], p["month"]))
        delta = (target.year - p["year"]) * 12 + (target.month - p["month"])
        if delta > 0:
            p = max(panels, key=lambda p: (p["year"], p["month"]))
        with in_frame(driver, frame):
            arr = js(driver, "return CT.arrows(arguments[0], arguments[1]);", p["panel"], p["header"])
        btn = arr["next"] if delta > 0 else arr["prev"]
        if btn is None:
            raise CalendarError(f"Could not find the {'next' if delta > 0 else 'previous'}-month "
                                f"arrow (detection: {arr['how']}, {arr['n']} candidates)")
        before = (p["year"], p["month"])
        LOG.debug("Calendar shows %s-%02d; clicking %s (found by %s)", before[0], before[1],
                  "next" if delta > 0 else "prev", arr["how"])
        with in_frame(driver, frame):
            safe_click(driver, guard, btn, "calendar month arrow")
        try:
            with in_frame(driver, frame):
                WebDriverWait(driver, 5, poll_frequency=0.3).until(
                    lambda d: {(q["year"], q["month"]) for q in calendar_panels(d)} != {before}
                    and calendar_panels(d))
        except TimeoutException:
            raise CalendarError(f"Month did not change after clicking the arrow (still {before[1]}/{before[0]});"
                                " the site may not allow that month")
    raise CalendarError("Too many month steps; calendar navigation is not converging")


def verify_via_calendar(driver, guard, frame, triggers, target: date) -> bool:
    panels = open_calendar(driver, guard, frame, triggers)
    if not panels:
        return False
    try:
        p = navigate_to_month(driver, guard, frame, target)
        dim = ((target.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        with in_frame(driver, frame):
            pick = js(driver, "return CT.pickDay(arguments[0], arguments[1], arguments[2]);", p["panel"], target.day, dim)
        return bool(pick.get("el") and pick.get("selected"))
    finally:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)


def select_date(driver, guard: NavigationGuard, target: date, debug: DebugRecorder) -> bool:
    """Select `target` in the site's date picker and verify it. Returns True if
    the date was changed, False if the page already showed it."""
    ev = date_evidence(driver, target)
    LOG.info("Page currently shows date: %s", ev["shown"] or "(not readable from the page)")
    if ev["shown"] == target and date_confirmed(ev, target)[0]:
        return False

    before = snapshot(driver, target)
    frame, triggers = find_triggers(driver)
    if not triggers:
        debug.dump(driver, f"no_date_trigger_{target}")
        raise CalendarError("Could not find the date-picker control. Run with --inspect and check "
                            "dateTriggers in the DOM report.")

    native = next((t for t in triggers if t.get("isInput") and t.get("inputType") == "date"), None)
    try:
        if native:
            LOG.info("Using native <input type=date>")
            with in_frame(driver, frame):
                driver.execute_script(JS_SET_NATIVE_DATE, native["el"], target.isoformat())
            guard.check("setting native date input")
        else:
            panels = open_calendar(driver, guard, frame, triggers)
            if not panels:
                raise CalendarError("Clicked the date-picker candidates but no calendar (month header + "
                                    "day grid) appeared")
            p = navigate_to_month(driver, guard, frame, target)
            dim = ((target.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
            with in_frame(driver, frame):
                pick = js(driver, "return CT.pickDay(arguments[0], arguments[1], arguments[2]);",
                          p["panel"], target.day, dim)
            if not pick.get("el"):
                raise CalendarError(f"Day {target.day} not found in the calendar grid ({pick.get('n')} day cells)")
            if pick.get("disabled"):
                raise CalendarError(f"{target} is disabled in the site's calendar — the site probably "
                                    "only offers a limited date range")
            LOG.debug("Clicking day cell %d (found by %s)", target.day, pick["how"])
            with in_frame(driver, frame):
                safe_click(driver, guard, pick["el"], f"calendar day {target}")
    except ScraperError:
        debug.dump(driver, f"calendar_failure_{target}")
        raise

    # ---- verify the date actually changed --------------------------------
    deadline, ok, why, ev = time.time() + 20, False, "", {}
    while time.time() < deadline:
        ev = date_evidence(driver, target)
        ok, why = date_confirmed(ev, target)
        if ok:
            break
        time.sleep(0.5)
    if not ok:
        frame, triggers = find_triggers(driver)
        if ev.get("shown") is None and triggers and verify_via_calendar(driver, guard, frame, triggers, target):
            LOG.info("Date verified via the calendar's selected cell")
        else:
            debug.dump(driver, f"date_not_verified_{target}")
            raise CalendarError(f"After selecting {target} the listing does not show it ({why}; "
                                f"date box={ev.get('shown')}, url={ev.get('url')}, row links={dict(ev.get('rows') or {})})")
    LOG.info("Selected date verified: %s (%s; url=%s; row-link dates=%s)", target, why,
             ev.get("url"), {str(k): v for k, v in (ev.get("rows") or {}).items()})
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()

    # ---- wait for the listing to refresh ----------------------------------
    old_fp = fingerprint(before)
    deadline, refreshed = time.time() + 20, False
    while time.time() < deadline:
        try:
            if before.first_row is not None:
                before.first_row.is_displayed()  # raises if re-rendered
        except StaleElementReferenceException:
            refreshed = True
            break
        if fingerprint(snapshot(driver, target)) != old_fp:
            refreshed = True
            break
        time.sleep(0.5)
    if not refreshed:
        LOG.warning("Listing did not visibly re-render after the date change; continuing after a settle wait")
    return True


# ---- Loading all flights ----------------------------------------------------
@dataclass
class DayScrape:
    page_date: date
    flights: list[RawFlight]
    text: str
    unparsed: list[str]
    earlier_exhausted: bool
    later_exhausted: bool
    covered_start: bool
    covered_end: bool
    notes: list[str] = field(default_factory=list)


def _dt_range(snap: Snapshot, page_date: date) -> tuple[Optional[datetime], Optional[datetime]]:
    dated = assign_dates(snap.flights, page_date, log=False) if snap.flights else []
    dts = [datetime.combine(d, r.sched_time, tzinfo=SGT) for r, d, _ in dated]
    return (min(dts), max(dts)) if dts else (None, None)


def load_more(driver, guard, page_date, word: str, done: Callable[[Snapshot], bool],
              max_clicks: int) -> tuple[Snapshot, bool]:
    """Click SHOW <word> FLIGHTS until coverage is reached, the control
    disappears, or two clicks in a row add nothing. Returns (snapshot, exhausted)."""
    snap = snapshot(driver, page_date)
    no_growth, clicks = 0, 0
    while clicks < max_clicks:
        if done(snap):
            return snap, False
        found = js(driver, f"return CT.showMore('{word}');")
        if not found:
            LOG.info("'Show %s flights' no longer offered after %d click(s)", word.lower(), clicks)
            return snap, True
        if found.get("href"):
            raise ListingLoadError(f"'Show {word.lower()} flights' is a link to {found['href']}; refusing to navigate")
        before = snapshot_keys(snap)
        safe_click(driver, guard, found["el"], f"show {word.lower()} flights")
        clicks += 1
        grew = False
        deadline = time.time() + 12
        while time.time() < deadline:
            time.sleep(0.6)
            snap = snapshot(driver, page_date)
            if len(snapshot_keys(snap) - before) > 0:
                grew = True
                break
        snap = wait_for_stable(driver, page_date, timeout=10)
        no_growth = 0 if grew else no_growth + 1
        LOG.debug("show %s click %d: %d rows parsed%s", word.lower(), clicks, len(snap.flights),
                  "" if grew else " (no new flights)")
        if no_growth >= 2:
            LOG.info("'Show %s flights' stopped adding flights; treating as exhausted", word.lower())
            return snap, True
    LOG.warning("Reached the %d-click limit for 'show %s flights'", max_clicks, word.lower())
    return snap, False


def check_terminal(raws: list[RawFlight], terminal: str, debug: Optional["DebugRecorder"] = None, driver=None) -> None:
    """Fail if the listing shows a different terminal from the one requested."""
    terms = Counter(r.terminal for r in raws if r.terminal)
    if not terms:
        LOG.warning("Rows show no terminal; cannot double-check that this is Terminal %s", terminal)
        return
    top, n = terms.most_common(1)[0]
    if top != terminal and n > terms.get(terminal, 0):
        if debug and driver:
            debug.dump(driver, f"wrong_terminal_T{top}")
        raise ListingLoadError(f"The page is showing Terminal {top} flights, not Terminal {terminal} "
                               f"({dict(terms)}). The site may have ignored the terminal setting.")


def scrape_day(driver, guard, cfg: Config, page_date: date, need_from: datetime, need_to: datetime,
               debug: DebugRecorder) -> DayScrape:
    LOG.info("— Scraping %s (need %s → %s) —", page_date, need_from.strftime("%d %b %H:%M"),
             need_to.strftime("%d %b %H:%M"))
    select_date(driver, guard, page_date, debug)
    wait_for_stable(driver, page_date)

    def have_start(s):
        lo, _ = _dt_range(s, page_date)
        return lo is not None and lo <= need_from

    def have_end(s):
        _, hi = _dt_range(s, page_date)
        return hi is not None and hi >= need_to

    snap, earlier_ex = load_more(driver, guard, page_date, "EARLIER", have_start, cfg.max_show_more_clicks)
    snap, later_ex = load_more(driver, guard, page_date, "LATER", have_end, cfg.max_show_more_clicks)
    snap = wait_for_stable(driver, page_date, timeout=10)
    guard.check("before capture")

    raw_dir = cfg.output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{cfg.file_prefix}_raw_listing_{page_date}.txt").write_text(snap.text, encoding="utf-8")
    check_terminal(snap.flights, cfg.terminal, debug, driver)

    lo, hi = _dt_range(snap, page_date)
    ds = DayScrape(page_date, snap.flights, snap.text, snap.unparsed, earlier_ex, later_ex,
                   covered_start=earlier_ex or have_start(snap), covered_end=later_ex or have_end(snap))
    LOG.info("%s: %d rows parsed (%s → %s); earlier exhausted=%s, later exhausted=%s",
             page_date, len(snap.flights), lo and lo.strftime("%d %b %H:%M"),
             hi and hi.strftime("%d %b %H:%M"), earlier_ex, later_ex)
    if snap.unparsed:
        ds.notes.append(f"{page_date}: {len(snap.unparsed)} row(s) could not be parsed (see log/raw text)")
        for u in snap.unparsed[:5]:
            LOG.warning("Unparsed row: %s", u)
    if not (ds.covered_start and ds.covered_end):
        ds.notes.append(f"{page_date}: listing coverage incomplete "
                        f"(start ok={ds.covered_start}, end ok={ds.covered_end})")
    return ds


# =============================================================================
# Pipeline
# =============================================================================
@dataclass
class RunResult:
    flights: list[Flight]
    buckets: dict[str, Counter]
    status: str
    notes: list[str]
    detail_csv: Path
    summary_csv: Path
    report: str
    documents: list = field(default_factory=list)


def finalise(cfg: Config, pages: list[tuple[date, list[RawFlight]]], notes: list[str],
             complete: bool, resolver: Optional[CountryResolver] = None) -> RunResult:
    """Pages → flights → dedupe → policies → aggregate → CSVs + report."""
    resolver = resolver or CountryResolver()
    intervals = build_intervals(cfg.report_date)
    flights: list[Flight] = []
    for page_date, raws in pages:
        page_flights = build_flights(raws, page_date, resolver, keep_date=page_date)
        if raws and not page_flights:
            raise IncompleteDataError(f"The listing captured for {page_date} contains no flights dated "
                                      f"{page_date}; the page probably showed a different date")
        flights.extend(page_flights)

    flights, merges = dedupe_flights(flights, cfg.codeshare_mode)
    for m in merges:
        LOG.info("Codeshare/duplicate merged: %s", m)
    apply_policies(flights, cfg, intervals)
    buckets = aggregate(flights, intervals)

    in_window = [f for f in flights if f.interval_label]
    status = "COMPLETE" if complete and not notes else ("PARTIAL — see notes" if not complete else "COMPLETE (with notes)")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if complete else "_PARTIAL"
    detail = cfg.output_dir / f"{cfg.file_prefix}_departures_{cfg.report_date}{suffix}.csv"
    summary = cfg.output_dir / f"{cfg.file_prefix}_country_summary_{cfg.report_date}{suffix}.csv"
    write_detail_csv(detail, in_window, cfg)
    write_summary_csv(summary, buckets)
    debug_all = cfg.output_dir / "debug" / f"all_parsed_{cfg.report_date}.csv"
    debug_all.parent.mkdir(parents=True, exist_ok=True)
    write_detail_csv(debug_all, flights, cfg)
    report = format_report(buckets, intervals, flights, cfg, status, notes, resolver.unknown)
    rt = build_report_table(buckets, intervals, cfg, status, notes, resolver.unknown)
    docs = write_report_documents(cfg, rt, suffix)
    save_report_json(cfg, rt, [detail, summary, *docs], suffix)
    return RunResult(flights, buckets, status, notes, detail, summary, report, docs)


def save_report_json(cfg: Config, rt: "ReportTable", files: list[Path], suffix: str = "") -> Path:
    """Machine-readable copy of the report (used by the desktop app's history list)."""
    from dataclasses import asdict
    folder = cfg.output_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cfg.file_prefix}_{cfg.report_date}{suffix}.json"
    data = asdict(rt)
    data.update({
        "terminal": cfg.terminal,
        "report_date": cfg.report_date.isoformat(),
        "generated_at": datetime.now(SGT).isoformat(timespec="seconds"),
        "files": {p.suffix.lstrip(".") if "summary" not in p.name else "summary_csv": p.name
                  for p in files if p and Path(p).exists()},
    })
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def run_live(cfg: Config) -> RunResult:
    debug = DebugRecorder(cfg.output_dir / "debug")
    start, end = reporting_window(cfg.report_date)
    next_day = cfg.report_date + timedelta(days=1)
    driver = build_driver(cfg)
    pages, notes, complete = [], [], True
    try:
        guard = open_departures(driver, cfg)
        try:
            main = scrape_day(driver, guard, cfg, cfg.report_date, start,
                              datetime.combine(next_day, dtime(0, 0), tzinfo=SGT), debug)
        except ScraperError:
            debug.dump(driver, "main_day_failure")
            raise
        pages.append((main.page_date, main.flights))
        notes += main.notes
        complete &= main.covered_start and main.covered_end
        try:
            nxt = scrape_day(driver, guard, cfg, next_day,
                             datetime.combine(next_day, dtime(0, 0), tzinfo=SGT), end, debug)
            pages.append((nxt.page_date, nxt.flights))
            notes += nxt.notes
            complete &= nxt.covered_start and nxt.covered_end
        except ScraperError as exc:
            debug.dump(driver, "next_day_failure")
            if not cfg.allow_partial:
                raise IncompleteDataError(f"Next-day ({next_day}) scrape failed: {exc}") from exc
            notes.append(f"Next-day scrape failed ({exc}); 00:00–02:00 interval is missing")
            complete = False
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not complete and not cfg.allow_partial:
        raise IncompleteDataError("Incomplete data: " + "; ".join(notes) +
                                  " (re-run, or use --allow-partial to write PARTIAL outputs)")
    return finalise(cfg, pages, notes, complete)


def run_replay(cfg: Config, raw_dir: Path) -> RunResult:
    pages, notes = [], []
    for d in (cfg.report_date, cfg.report_date + timedelta(days=1)):
        p = raw_dir / f"{cfg.file_prefix}_raw_listing_{d}.txt"
        if not p.exists():
            p = raw_dir / f"raw_listing_{d}.txt"  # files saved by older versions
        if not p.exists():
            notes.append(f"missing {p.name}")
            continue
        res = parse_listing_text(p.read_text(encoding="utf-8"), d)
        LOG.info("Replayed %s: %d rows parsed, %d unparsed", p.name, len(res.flights), len(res.unparsed_rows))
        pages.append((d, res.flights))
    if len(pages) < 2 and not cfg.allow_partial:
        raise IncompleteDataError("Replay needs both raw files: " + ", ".join(notes))
    return finalise(cfg, pages, notes, complete=len(pages) == 2)


def run_inspect(cfg: Config) -> Path:
    """Diagnostics for adapting to the live DOM: writes a report, screenshots and
    the calendar's HTML. Makes no assumptions that could navigate away."""
    debug = DebugRecorder(cfg.output_dir / "debug")
    driver = build_driver(cfg)
    try:
        guard = open_departures(driver, cfg)
        rep = dom_report(driver)
        out = cfg.output_dir / "debug" / "inspect_report.json"
        frame, triggers = find_triggers(driver)
        panels = open_calendar(driver, guard, frame, triggers) if triggers else []
        rep["calendar_opened"] = bool(panels)
        if panels:
            with in_frame(driver, frame):
                p = panels[0]
                arr = js(driver, "return CT.arrows(arguments[0], arguments[1]);", p["panel"], p["header"])
                rep["calendar_arrows"] = {"how": arr["how"], "next": js(driver, "return CT.describe(arguments[0]);", arr["next"]),
                                          "prev": js(driver, "return CT.describe(arguments[0]);", arr["prev"])}
                rep["calendar_html"] = driver.execute_script("return arguments[0].outerHTML;", p["panel"])[:60000]
            debug.dump(driver, "inspect_calendar_open")
        out.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        debug.dump(driver, "inspect_page")
        LOG.info("Inspection report written to %s", out)
        return out
    finally:
        driver.quit()


# =============================================================================
# CLI
# =============================================================================
def setup_logging(output_dir: Path, level: str) -> Path:
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(getattr(logging, level.upper()))
    ch.setFormatter(fmt)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(ch)
    root.addHandler(fh)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Changi departures by destination country (06:00 → 02:00 next day, SGT)")
    ap.add_argument("--terminal", choices=TERMINALS, default="1", help="Changi terminal (default 1)")
    ap.add_argument("--browser", choices=["auto", "chrome", "edge"], default="auto",
                    help="Browser to drive (auto: Chrome if installed, otherwise Microsoft Edge)")
    ap.add_argument("--date", help="Report date YYYY-MM-DD (default: today in Singapore)")
    ap.add_argument("--visible", action="store_true", help="Show the Chrome window")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--cancelled", choices=["exclude", "include"], default="exclude",
                    help="Count cancelled flights? (default: exclude; they stay in the CSV as counted=no)")
    ap.add_argument("--time-basis", choices=["scheduled", "estimated"], default="scheduled",
                    help="Bucket by scheduled time (default) or estimated/retimed time when shown")
    ap.add_argument("--codeshare", choices=["auto", "strict"], default="auto",
                    help="auto: merge same time+destination rows of different carriers; strict: only shared flight numbers")
    ap.add_argument("--allow-partial", action="store_true", help="Write *_PARTIAL outputs instead of failing")
    ap.add_argument("--doc-format", choices=["both", "docx", "pdf", "none"], default="both",
                    help="Readable report document(s) to write (default: both Word and PDF)")
    ap.add_argument("--inspect", action="store_true", help="Write DOM diagnostics and exit")
    ap.add_argument("--replay-raw", metavar="DIR", help="Re-parse saved raw_listing_*.txt files (no browser)")
    ap.add_argument("--url", default=None, help="Override the departures page URL (testing)")
    ap.add_argument("--timeout", type=int, default=45, help="Seconds to wait for the listing")
    ap.add_argument("--chrome-binary", help="Path to chrome.exe (normally not needed)")
    ap.add_argument("--chromedriver", help="Path to chromedriver.exe (normally not needed)")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    report_date = date.fromisoformat(args.date) if args.date else today_in_singapore()
    cfg = Config(
        report_date=report_date, url=args.url, terminal=args.terminal, browser=args.browser,
        headless=not args.visible,
        output_dir=Path(args.output_dir), cancelled_policy=args.cancelled,
        time_basis=args.time_basis, codeshare_mode=args.codeshare,
        allow_partial=args.allow_partial, page_timeout=args.timeout, doc_format=args.doc_format,
        chrome_binary=args.chrome_binary, chromedriver=args.chromedriver,
    )
    log_file = setup_logging(cfg.output_dir, args.log_level)
    LOG.info("Report date %s (Singapore). Log file: %s", report_date, log_file)
    try:
        if args.inspect:
            run_inspect(cfg)
            return 0
        result = run_replay(cfg, Path(args.replay_raw)) if args.replay_raw else run_live(cfg)
    except ScraperError as exc:
        LOG.error("%s: %s", type(exc).__name__, exc)
        LOG.error("No report was produced. Debug files are in %s", cfg.output_dir / "debug")
        return 2
    print(result.report)
    print(f"\nFlight records : {result.detail_csv}")
    print(f"Country summary: {result.summary_csv}")
    for d in result.documents:
        print(f"Report document: {d}")
    return 0 if result.status.startswith("COMPLETE") else 3


if __name__ == "__main__":
    sys.exit(main())
