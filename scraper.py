import re
import asyncio
from bs4 import BeautifulSoup
import httpx
from fake_useragent import UserAgent

# Uncomment if you have a cache module:
# from cache import get_cache, set_cache

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
    FIX: The old code matched nav links like /cricket-match/live-scores (no numeric ID).
    We now require a numeric segment (4+ digits) in the href to confirm it's a real match.
    We also extract team names and status from the surrounding card markup.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = []
    seen = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]

        # --- KEY FIX: must contain a numeric match ID ---
        if not re.search(r'/\d{4,}/', href):
            continue

        # Must be a match/scores link (not a player or series link)
        if not any(kw in href for kw in [
            "/cricket-match/",
            "/live-cricket-scores/",
            "/cricket-scores/",
        ]):
            continue

        if href in seen:
            continue
        seen.add(href)

        match_id = extract_match_id(href)
        if not match_id:
            continue

        # Walk up to the match card container to scrape richer data
        card_el = _find_card_ancestor(link)

        team1, team2 = _extract_teams(card_el or link)
        status = _extract_status(card_el or link)
        title = _extract_title(card_el or link, href)
        match_date, match_time = _extract_datetime(card_el or link)

        cards.append({
            "match_id": match_id,
            "title": title,
            "match_url": BASE + href if href.startswith("/") else href,
            "team1": team1,
            "team2": team2,
            "status": status,
            "date": match_date,
            "time": match_time,
        })

    return cards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_card_ancestor(tag):
    """
    Walk up the DOM to find a container div/li that holds the full match card.
    Cricbuzz wraps each match in a <div> with class containing 'cb-mtch-lst' or similar.
    """
    card_classes = {
        "cb-mtch-lst", "cb-col", "cb-lst-itm", "cb-match-card",
        "cb-scr-wll-chvrn",  # live score widget
    }
    node = tag.parent
    for _ in range(8):  # max 8 levels up
        if node is None or node.name in ("body", "html", "[document]"):
            break
        classes = set(node.get("class") or [])
        if classes & card_classes:
            return node
        node = node.parent
    return None


def _extract_teams(el):
    """Extract team names from card element."""
    text = el.get_text(" ", strip=True)

    # Cricbuzz often has "TeamA vs TeamB" somewhere in the card
    m = re.search(r'([A-Za-z ]+?)\s+(?:vs?\.?)\s+([A-Za-z ]+?)(?:\s*,|\s*\d|\s*-|$)', text, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    return "", ""


def _extract_status(el):
    """Extract match status (e.g. 'Live', 'Stumps', result line)."""
    text = el.get_text(" ", strip=True)

    # Look for common Cricbuzz status patterns - prioritize them
    status_patterns = [
        (r'\b(Live|LIVE|LIVE NOW)\b', lambda m: m.group(1)),
        (r'\b(won|leads|trail|need|require|innings|stumps|draw|tied|no result)\b', lambda m: m.group(1).capitalize()),
        (r'(\d+\s*(?:runs?|wickets?|wkts?)\s+(?:needed|required))', lambda m: m.group(1)),
    ]
    
    for pattern, formatter in status_patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return formatter(m)
    
    # If no recognized pattern, check for common result keywords
    if any(kw in text.lower() for kw in ["result", "won by", "tied", "victory"]):
        return "Completed"
    
    # Return first 60 chars if something is there
    return text[:60] if text else ""


def _extract_datetime(el):
    """Extract match date and time from card element."""
    text = el.get_text(" ", strip=True)
    
    match_date = ""
    match_time = ""
    
    # Extract time (e.g., "4:00 PM", "16:00", "4:00 pm", "2:30 AM")
    time_pattern = r'(\d{1,2}):(\d{2})\s*(AM|PM|am|pm|IST|UTC|GMT)?'
    time_match = re.search(time_pattern, text)
    if time_match:
        match_time = time_match.group(0).strip()
    
    # Extract date patterns
    # Patterns: "May 22", "22 May", "May 22, 2024", "22 May 2024", etc.
    date_patterns = [
        r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?:\s+\d{4})?)',  # "22 May" or "22 May 2024"
        r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?)',  # "May 22" or "May 22, 2024"
    ]
    
    for pattern in date_patterns:
        date_match = re.search(pattern, text, re.I)
        if date_match:
            match_date = date_match.group(1).strip()
            break
    
    return match_date, match_time


def _extract_title(el, href):
    """Build a clean title from the element text or href slug."""
    text = el.get_text(" ", strip=True)
    if text:
        # Truncate very long card text
        return text[:120]

    # Fallback: humanise the slug from the URL
    slug_m = re.search(r'/(?:cricket-match|live-cricket-scores)/([a-z0-9-]+)/', href)
    if slug_m:
        return slug_m.group(1).replace("-", " ").title()

    return href


# ---------------------------------------------------------------------------
# Public API (same interface as before)
# ---------------------------------------------------------------------------

async def get_live_matches():
    """Fetch currently live cricket matches"""
    url = f"{BASE}/cricket-match/live-scores"
    html = await fetch(url)
    matches = parse_match_cards(html)
    
    # Filter to only live matches
    live_matches = [m for m in matches if m.get("status") and "live" in m.get("status", "").lower()]
    
    return live_matches if live_matches else matches


async def get_upcoming_matches():
    """Fetch upcoming cricket matches (scheduled but not started)"""
    # cache = get_cache("upcoming", ttl=300)
    # if cache: return cache

    url = f"{BASE}/cricket-match/live-scores/upcoming-matches"
    html = await fetch(url)
    data = parse_match_cards(html)
    
    # Filter to only upcoming matches (not live, not recent)
    upcoming = [m for m in data if m.get("status") and not any(
        kw in m.get("status", "").lower() for kw in ["live", "won", "tied", "no result"]
    )]

    # set_cache("upcoming", upcoming)
    return upcoming if upcoming else data


async def get_recent_matches():
    """Fetch recently completed cricket matches (results)"""
    # cache = get_cache("recent", ttl=300)
    # if cache: return cache

    url = f"{BASE}/cricket-match/live-scores/recent-matches"
    html = await fetch(url)
    data = parse_match_cards(html)

    # Filter to only recently completed matches
    recent = [m for m in data if m.get("status") and any(
        kw in m.get("status", "").lower() for kw in ["won", "tied", "result", "stumps", "innings"]
    )]

    # set_cache("recent", recent)
    return recent if recent else data


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
    # Try cache first (optional)
    # cache_key = f"squad_{match_id}"
    # cache = get_cache(cache_key, ttl=86400)
    # if cache: return cache

    teams = []
    api_attempts = []
    # set_cache(cache_key, result)
    url = f"{BASE}/cricket-match-squads/{match_id}"
    html = await fetch(url)
    data = fetch_squads(html)
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
