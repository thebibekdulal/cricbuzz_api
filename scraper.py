import re
import json
import os
import time
import asyncio
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import httpx
from fake_useragent import UserAgent

BASE = "https://www.cricbuzz.com"

# ---------------------------------------------------------------------------
# Cache configuration
# ---------------------------------------------------------------------------
CACHE_DIR = ".cache"

# TTL in seconds for each endpoint.
# Remove or comment out an entry to bypass the cache for that endpoint.
CACHE_TTL = {
    "live_matches":     60 * 60,   # 1 hour
    "recent_matches":   60 * 60,   # 1 hour
    "upcoming_matches": 60 * 60,   # 1 hour
    "squads":           60 * 60,   # 1 hour
    # "scorecard" is intentionally absent — always fetched fresh
}

ua = UserAgent()

HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
}


def headers():
    h = HEADERS.copy()
    h["User-Agent"] = ua.random
    return h


# ---------------------------------------------------------------------------
# File-based cache helpers
# ---------------------------------------------------------------------------

def _get_ttl(cache_key: str) -> int:
    """Return TTL for cache_key by matching against CACHE_TTL prefixes."""
    for name, ttl in CACHE_TTL.items():
        if cache_key == name or cache_key.startswith(name + "_"):
            return ttl
    return 0  # not in table → never cache


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^\w-]", "_", key)
    return os.path.join(CACHE_DIR, f"{safe}.json")


def _cache_read(key: str):
    ttl = _get_ttl(key)
    if not ttl:
        return None
    try:
        with open(_cache_path(key)) as f:
            entry = json.load(f)
        if time.time() - entry["ts"] < ttl:
            return entry["data"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        pass
    return None


def _cache_write(key: str, data) -> None:
    if not _get_ttl(key):
        return
    with open(_cache_path(key), "w") as f:
        json.dump({"ts": time.time(), "data": data}, f)


async def fetch(url: str):
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers=headers()
    ) as client:
        last_error = None
        for attempt in range(3):
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.text
                elif r.status_code in (403, 429):
                    # Rate limited or blocked
                    last_error = f"Status {r.status_code}: Access denied or rate limited"
                    await asyncio.sleep(2 ** attempt)
                else:
                    last_error = f"Status {r.status_code}"
                    await asyncio.sleep(2 ** attempt)
            except httpx.TimeoutException:
                last_error = "Timeout"
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(2 ** attempt)

    raise Exception(f"Unable to fetch {url}. Last error: {last_error}")


def extract_match_id(url):
    # Real match URLs look like: /cricket-match/india-vs-aus/98765/live-cricket-scores
    # OR: /live-cricket-scores/98765/...
    m = re.search(r'/(\d{4,})', url)
    return m.group(1) if m else ""


def parse_match_cards(html):
    """
    Cricbuzz (Next.js/Tailwind) stores match status in the <a> title attribute:
      "Nepal vs United States of America, 109th Match - NEP Won "
    All three pages (live/recent/upcoming) share the same listing section;
    status values drive the filtering in each endpoint.
    """
    soup = BeautifulSoup(html, "lxml")

    # Drop the nav ticker (dark top bar) to avoid counting those links twice
    for ticker in soup.find_all("div", class_=re.compile(r"bg-\[#4a4a4a\]")):
        ticker.decompose()

    cards = []
    seen_ids = set()  # deduplicate by match_id — same match can have different URL slugs

    for link in soup.find_all("a", href=re.compile(r"/live-cricket-scores/\d{4,}/")):
        href = link["href"]

        match_id = extract_match_id(href)
        if not match_id or match_id in seen_ids:
            continue
        seen_ids.add(match_id)

        # title attr format: "Team1 vs Team2, Match Desc - STATUS "
        title_attr = link.get("title", "").strip()
        if " - " in title_attr:
            title_part, status = title_attr.rsplit(" - ", 1)
            status = status.strip()
        else:
            title_part, status = title_attr, ""

        team1, team2 = "", ""
        vs_m = re.match(r"^(.+?)\s+vs\s+(.+?)(?:,|$)", title_part, re.I)
        if vs_m:
            team1 = vs_m.group(1).strip()
            team2 = vs_m.group(2).strip()

        # Fallback title from URL slug when title attr is absent
        if not title_part:
            slug_m = re.search(r"/live-cricket-scores/\d+/([^/]+)", href)
            title_part = slug_m.group(1).replace("-", " ").title() if slug_m else href

        cards.append({
            "match_id": match_id,
            "title": title_part.strip(),
            "match_url": BASE + href if href.startswith("/") else href,
            "team1": team1,
            "team2": team2,
            "status": status,
        })

    return cards


# ---------------------------------------------------------------------------
# Status classifiers
# ---------------------------------------------------------------------------

def _is_completed(status: str) -> bool:
    s = status.strip().lower()
    return bool(re.search(r"\bwon\b", s)) or s in {"complete", "tied", "no result", "abandoned"}

def _is_upcoming(status: str) -> bool:
    s = status.strip().lower()
    return s == "preview" or s.startswith("upcoming")

def _is_live(status: str) -> bool:
    # The website's Live tab shows everything on the live-scores page except Preview.
    # This includes Toss, Stumps, score text, recently completed (Won/Complete), etc.
    return not _is_upcoming(status)


# ---------------------------------------------------------------------------
# Public API (same interface as before)
# ---------------------------------------------------------------------------

def _parse_utc_datetime(full_text: str) -> str:
    """
    Extract a UTC ISO 8601 datetime string from Cricbuzz match page text.

    Priority:
      1. "Match starts at May 23, 11:50 GMT"  — date + 24h GMT time in one line
      2. Separate GMT time ("11:50 AM GMT") + date from "Date & Time:" field
    Returns "YYYY-MM-DDTHH:MM:SSZ" or "" on failure.
    """
    # Pattern 1: full date + time in one phrase (24h, no AM/PM)
    m = re.search(
        r'Match starts at\s+(\w+)\s+(\d{1,2}),?\s+(\d{1,2}:\d{2})\s*(?:AM|PM)?\s*GMT',
        full_text, re.I,
    )
    if m:
        return _build_utc_iso(m.group(1), m.group(2), m.group(3), "")

    # Pattern 2: "11:50 AM GMT" anywhere on the page
    time_m = re.search(r'(\d{1,2}:\d{2})\s*(AM|PM)\s*GMT', full_text, re.I)
    date_m = re.search(
        r'(?:Date\s*[&]\s*Time:|Match starts at)\s*(?:\w+,\s*)?(\w+)\s+(\d{1,2})',
        full_text, re.I,
    )
    if time_m and date_m:
        return _build_utc_iso(date_m.group(1), date_m.group(2), time_m.group(1), time_m.group(2))

    return ""


def _build_utc_iso(month_str: str, day_str: str, time_str: str, ampm: str) -> str:
    try:
        now = datetime.now(timezone.utc)
        if ampm:
            dt = datetime.strptime(
                f"{month_str} {day_str} {now.year} {time_str} {ampm.upper()}",
                "%B %d %Y %I:%M %p",
            )
        else:
            dt = datetime.strptime(
                f"{month_str} {day_str} {now.year} {time_str}",
                "%B %d %Y %H:%M",
            )
        dt = dt.replace(tzinfo=timezone.utc)
        # If the parsed date is more than 60 days in the past, it's next year
        if (now - dt).days > 60:
            dt = dt.replace(year=now.year + 1)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


async def get_match_details(match_url: str) -> dict:
    """Fetch series, venue, and UTC start time from an individual match page."""
    try:
        html = await fetch(match_url)
        soup = BeautifulSoup(html, "lxml")

        details = {"series": "", "venue": "", "start_time_utc": ""}

        # Series and Venue appear together: "Series: X • Venue: Y •"
        for el in soup.find_all(True):
            text = el.get_text(" ", strip=True)
            if "Series:" in text and "Venue:" in text and len(text) < 500:
                series_m = re.search(r'Series:\s*(.+?)(?:\s*[•·]\s*Venue:|$)', text)
                if series_m:
                    details["series"] = series_m.group(1).strip()
                venue_m = re.search(r'Venue:\s*(.+?)(?:\s*[•·]|$)', text)
                if venue_m:
                    details["venue"] = venue_m.group(1).strip()
                break

        # Prefer JSON-LD SportsEvent startDate (already UTC ISO 8601)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "SportsEvent":
                    start_date = data.get("startDate", "")
                    if start_date:
                        # Normalize "2026-05-08T04:00:00.000Z" → "2026-05-08T04:00:00Z"
                        details["start_time_utc"] = re.sub(r'\.\d+Z$', 'Z', start_date)
                        break
            except (json.JSONDecodeError, AttributeError):
                continue

        # Fallback to regex text parsing if JSON-LD not present
        if not details["start_time_utc"]:
            details["start_time_utc"] = _parse_utc_datetime(soup.get_text(" "))

        return details
    except Exception:
        return {"series": "", "venue": "", "start_time_utc": ""}


async def _enrich_with_details(matches: list) -> list:
    """Fetch individual match pages in parallel and add series/venue/date_time."""
    tasks = [get_match_details(m["match_url"]) for m in matches]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for match, detail in zip(matches, results):
        if isinstance(detail, dict):
            match.update(detail)
    return matches


async def get_live_matches():
    cached = _cache_read("live_matches")
    if cached is not None:
        return cached
    url = f"{BASE}/cricket-match/live-scores"
    html = await fetch(url)
    matches = parse_match_cards(html)
    live = [m for m in matches if _is_live(m.get("status", ""))]
    result = await _enrich_with_details(live)
    _cache_write("live_matches", result)
    return result


async def get_upcoming_matches():
    cached = _cache_read("upcoming_matches")
    if cached is not None:
        return cached
    url = f"{BASE}/cricket-match/live-scores/upcoming-matches"
    html = await fetch(url)
    data = parse_match_cards(html)
    upcoming = [m for m in data if _is_upcoming(m.get("status", "")) or not m.get("status", "").strip()]
    result = await _enrich_with_details(upcoming)
    _cache_write("upcoming_matches", result)
    return result


async def get_recent_matches():
    cached = _cache_read("recent_matches")
    if cached is not None:
        return cached
    url = f"{BASE}/cricket-match/live-scores/recent-matches"
    html = await fetch(url)
    data = parse_match_cards(html)
    recent = [m for m in data if _is_completed(m.get("status", "")) or not m.get("status", "").strip()]
    result = await _enrich_with_details(recent)
    _cache_write("recent_matches", result)
    return result


async def get_scorecard(match_id):
    """Fetch scorecard data from Cricbuzz API - returns full scorecard"""
    url = f"{BASE}/api/mcenter/scorecard/{match_id}"
    
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers()) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                return data
            else:
                return {
                    "error": f"HTTP {response.status_code}",
                    "match_id": match_id
                }
    except Exception as e:
        return {
            "error": str(e),
            "match_id": match_id
        }



async def get_squads(match_id):
    cache_key = f"squads_{match_id}"
    cached = _cache_read(cache_key)
    if cached is not None:
        return cached
    url = f"{BASE}/cricket-match-squads/{match_id}"
    html = await fetch(url)
    data = fetch_squads(html)
    _cache_write(cache_key, data)
    return data


_ROLE_KEYWORDS = [
    ("wicketkeeper", "wicket_keeper"),
    ("wicket keeper", "wicket_keeper"),
    ("keeper", "wicket_keeper"),
    ("bowling allrounder", "all_rounder"),
    ("batting allrounder", "all_rounder"),
    ("all-rounder", "all_rounder"),
    ("allrounder", "all_rounder"),
    ("all rounder", "all_rounder"),
    ("bowler", "bowler"),
    ("pace", "bowler"),
    ("spin", "bowler"),
    ("batter", "batter"),
    ("batsman", "batter"),
    ("opening", "batter"),
]


def categorize_role(role):
    role_lower = (role or "").lower().strip()
    for keyword, category in _ROLE_KEYWORDS:
        if keyword in role_lower:
            return category
    return "other"


def _group_by_category(players):
    groups = {
        "batters": [],
        "bowlers": [],
        "all_rounders": [],
        "wicket_keepers": [],
        "other": [],
    }
    category_to_key = {
        "batter": "batters",
        "bowler": "bowlers",
        "all_rounder": "all_rounders",
        "wicket_keeper": "wicket_keepers",
        "other": "other",
    }
    for p in players:
        key = category_to_key.get(p["category"], "other")
        groups[key].append(p)
    return {k: v for k, v in groups.items() if v}


def fetch_squads(html):
    soup = BeautifulSoup(html, "html.parser")

    # Full country names sit in the main h1: "Nepal vs United States of America, ..."
    teams = []
    for h1 in soup.find_all("h1"):
        txt = h1.get_text(strip=True)
        m = re.search(r"([A-Za-z &]+?)\s+vs\s+([A-Za-z &]+?)(?:,|\s+\d)", txt)
        if m:
            teams = [m.group(1).strip(), m.group(2).strip()]
            break
    if not teams:
        teams = ["Team A", "Team B"]

    players = []
    for a in soup.select('a[href*="/profiles/"]'):
        span = a.find("span")
        nm = span.get_text(strip=True) if span else a.get_text(strip=True)
        if not nm:
            continue

        role_tag = a.find("div", class_=re.compile(r"cbTxtSec|text-xs", re.I))
        role = role_tag.get_text(strip=True) if role_tag else ""

        img_tag = a.find("img")
        image_url = None
        if img_tag:
            src = img_tag.get("src") or img_tag.get("data-src") or ""
            if src and "player-default" not in src:
                image_url = src if src.startswith("http") else f"https://www.cricbuzz.com{src}"

        players.append({
            "name": nm,
            "role": role,
            "category": categorize_role(role),
            "image_url": image_url,
        })

    half = len(players) // 2
    team2 = teams[1] if len(teams) > 1 else "Team B"
    return {
        teams[0]: {"players": _group_by_category(players[:half])},
        team2: {"players": _group_by_category(players[half:])},
    }
