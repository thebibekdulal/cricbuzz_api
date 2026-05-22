import re
import asyncio
from datetime import datetime, timezone
from bs4 import BeautifulSoup
import httpx
from fake_useragent import UserAgent

BASE = "https://www.cricbuzz.com"

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
    m = re.search(r'/(\d{4,})', url)
    return m.group(1) if m else ""


async def _fetch_match_datetime(match_url: str):
    """
    Backup Fallback: Visits individual sub-pages if text data is 
    missing entirely from the parent dashboard listing feed.
    """
    match_date = ""
    match_time = ""

    try:
        html = await fetch(match_url)
        soup = BeautifulSoup(html, "lxml")

        for script in soup.find_all("script"):
            text = script.string or ""
            ts_match = re.search(r'"(startTime|matchStartTime|startdate)"\s*:\s*"?(\d{10,13})"?', text)
            if ts_match:
                ts = int(ts_match.group(2))
                ts_sec = ts / 1000 if ts > 1e10 else ts
                try:
                    dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
                    match_date = dt.strftime("%-d %b %Y")
                    match_time = dt.strftime("%I:%M %p UTC").lstrip("0")
                    return match_date, match_time
                except (OSError, OverflowError, ValueError):
                    pass

        timestamp_attrs = ("data-start-time", "data-dtz", "data-timestamp")
        for node in soup.find_all(True):
            for attr in timestamp_attrs:
                val = node.get(attr, "")
                if val and str(val).isdigit() and len(str(val)) >= 10:
                    ts = int(val)
                    ts_sec = ts / 1000 if ts > 1e10 else ts
                    try:
                        dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
                        match_date = dt.strftime("%-d %b %Y")
                        match_time = dt.strftime("%I:%M %p UTC").lstrip("0")
                        return match_date, match_time
                    except (OSError, OverflowError, ValueError):
                        pass

        for meta in soup.find_all("meta"):
            content = meta.get("content", "")
            MONTHS = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
            date_patterns = [
                rf'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+(\d{{1,2}}\s+{MONTHS}(?:\s+\d{{4}})?)',
                rf'(\d{{1,2}}\s+{MONTHS}(?:\s+\d{{4}})?)',
                rf'({MONTHS}\s+\d{{1,2}}(?:,?\s+\d{{4}})?)',
            ]
            for pattern in date_patterns:
                dm = re.search(pattern, content, re.I)
                if dm:
                    match_date = dm.group(1).strip()
                    break
            time_match = re.search(
                r'\b(\d{1,2}:\d{2}(?:\s*(?:AM|PM|am|pm))?(?:\s*(?:IST|UTC|GMT|EST|PST))?)\b',
                content
            )
            if time_match:
                match_time = time_match.group(1).strip()
            if match_date and match_time:
                return match_date, match_time

    except Exception:
        pass

    return match_date, match_time


async def _enrich_with_datetime(matches: list) -> list:
    """Fetch datetime concurrently only if records lack data structural values."""
    async def enrich(match):
        if not match["date"] or match["time"] == "TBD":
            date, time = await _fetch_match_datetime(match["match_url"])
            if date: match["date"] = date
            if time: match["time"] = time
        return match

    return await asyncio.gather(*[enrich(m) for m in matches])


def parse_match_cards(html):
    """
    Main extraction parser. Targets row item layouts directly 
    and checks inner layout strings where metadata sits natively.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = []
    seen = set()

    for match_box in soup.select("div.cb-mtch-lst-itm, div.cb-scr-wll-chvrn, div.cb-col-100.cb-col"):
        link = match_box.find("a", href=True) if match_box.name != "a" else match_box
        if not link:
            continue

        href = link["href"]
        if not re.search(r'/\d{4,}/', href) or href in seen:
            continue

        if not any(kw in href for kw in [
            "/cricket-match/",
            "/live-cricket-scores/",
            "/cricket-scores/",
        ]):
            continue

        seen.add(href)
        match_id = extract_match_id(href)
        if not match_id:
            continue

        card_text = match_box.get_text(" ", strip=True)
        
        team1, team2 = _extract_teams(match_box)
        status = _extract_status(match_box)
        title = _extract_title(match_box, href)

        match_date = ""
        match_time = ""
        
        MONTHS = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
        date_m = re.search(rf'({MONTHS}\s+\d{{1,2}}|\d{{1,2}}\s+{MONTHS})', card_text, re.I)
        if date_m:
            match_date = date_m.group(1).strip()
            
        time_m = re.search(r'\b(\d{{1,2}}:\d{{2}}\s*(?:AM|PM|am|pm))\b', card_text)
        if time_m:
            match_time = time_m.group(1).strip()

        cards.append({
            "match_id": match_id,
            "title": title,
            "match_url": BASE + href if href.startswith("/") else href,
            "team1": team1,
            "team2": team2,
            "status": status,
            "date": match_date if match_date else "Today",
            "time": match_time if match_time else "TBD",
        })

    return cards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_teams(el):
    """Parses clean team text while discarding scores, trailing live indicators, and metadata."""
    text = el.get_text(" ", strip=True)
    
    # Extract structural match patterns before tracking layout extras
    m = re.search(r'([A-Za-z ]+?)\s+(?:vs?\.?)\s+([A-Za-z ]+?)(?:\s*,|\s*\d|\s*-|$)', text, re.I)
    if m:
        t1 = m.group(1).strip()
        t2 = m.group(2).strip()
        
        # Clean out any trailing layout text noise such as "Live" or structural indicators
        t1 = re.sub(r'\b(live|vs)\b.*$', '', t1, flags=re.I).strip()
        t2 = re.sub(r'\b(live|vs)\b.*$', '', t2, flags=re.I).strip()
        return t1, t2
        
    return "", ""


def _extract_status(el):
    text = el.get_text(" ", strip=True)

    status_patterns = [
        (r'\b(Live|LIVE|LIVE NOW)\b', lambda m: m.group(1)),
        (r'\b(won|leads|trail|need|require|innings|stumps|draw|tied|no result)\b', lambda m: m.group(1).capitalize()),
        (r'(\d+\s*(?:runs?|wickets?|wkts?)\s+(?:needed|required))', lambda m: m.group(1)),
    ]

    for pattern, formatter in status_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return formatter(m)

    if any(kw in text.lower() for kw in ["result", "won by", "tied", "victory"]):
        return "Completed"

    return text[:60] if text else ""


def _extract_title(el, href):
    text = el.get_text(" ", strip=True)
    if text:
        return text[:120]
    slug_m = re.search(r'/(?:cricket-match|live-cricket-scores)/([a-z0-9-]+)/', href)
    if slug_m:
        return slug_m.group(1).replace("-", " ").title()
    return href


# ---------------------------------------------------------------------------
# Public API Endpoints
# ---------------------------------------------------------------------------

async def get_live_matches():
    url = f"{BASE}/cricket-match/live-scores"
    html = await fetch(url)
    matches = parse_match_cards(html)
    live_matches = [m for m in matches if m.get("status") and "live" in m.get("status", "").lower()]
    result = live_matches if live_matches else matches
    return await _enrich_with_datetime(result)


async def get_upcoming_matches():
    url = f"{BASE}/cricket-match/live-scores/upcoming-matches"
    html = await fetch(url)
    data = parse_match_cards(html)
    upcoming = [m for m in data if m.get("status") and not any(
        kw in m.get("status", "").lower() for kw in ["live", "won", "tied", "no result"]
    )]
    result = upcoming if upcoming else data
    return await _enrich_with_datetime(result)


async def get_recent_matches():
    url = f"{BASE}/cricket-match/live-scores/recent-matches"
    html = await fetch(url)
    data = parse_match_cards(html)
    recent = [m for m in data if m.get("status") and any(
        kw in m.get("status", "").lower() for kw in ["won", "tied", "result", "stumps", "innings"]
    )]
    result = recent if recent else data
    return await _enrich_with_datetime(result)


async def get_scorecard(match_id):
    url = f"{BASE}/api/mcenter/scorecard/{match_id}"
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers()) as client:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"HTTP {response.status_code}", "match_id": match_id}
    except Exception as e:
        return {"error": str(e), "match_id": match_id}


async def get_squads(match_id):
    url = f"{BASE}/cricket-match-squads/{match_id}"
    html = await fetch(url)
    return fetch_squads(html)


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
