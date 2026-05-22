import random
import re
import asyncio
from bs4 import BeautifulSoup
import httpx
from fake_useragent import UserAgent
from cache import get_cache, set_cache

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
        for attempt in range(3):
            try:
                r = await client.get(url)

                if r.status_code == 200:
                    return r.text

                await asyncio.sleep(2 ** attempt)

            except Exception:
                await asyncio.sleep(2 ** attempt)

    raise Exception("Unable to fetch Cricbuzz")


def extract_match_id(url):
    m = re.search(r'/(\d+)/', url)
    return m.group(1) if m else ""


def parse_match_cards(html):
    soup = BeautifulSoup(html, "lxml")
    cards = []

    links = soup.find_all("a", href=True)

    seen = set()

    for link in links:
        href = link["href"]

        if "/cricket-match/" not in href:
            continue

        if href in seen:
            continue

        seen.add(href)

        text = link.get_text(" ", strip=True)

        if not text:
            continue

        cards.append({
            "match_id": extract_match_id(href),
            "title": text,
            "match_url": BASE + href,
            "team1": "",
            "team2": "",
            "status": "",
        })

    return cards


async def get_live_matches():
    url = f"{BASE}/cricket-match/live-scores"
    html = await fetch(url)

    return parse_match_cards(html)


async def get_upcoming_matches():
    cache = get_cache("upcoming", ttl=300)

    if cache:
        return cache

    url = f"{BASE}/cricket-match/live-scores/upcoming-matches"
    html = await fetch(url)

    data = parse_match_cards(html)

    set_cache("upcoming", data)

    return data


async def get_recent_matches():
    cache = get_cache("recent", ttl=300)

    if cache:
        return cache

    url = f"{BASE}/cricket-match/live-scores/recent-matches"
    html = await fetch(url)

    data = parse_match_cards(html)

    set_cache("recent", data)

    return data


async def get_scorecard(match_id):
    url = f"{BASE}/live-cricket-scorecard/{match_id}"
    html = await fetch(url)

    soup = BeautifulSoup(html, "lxml")

    innings = []

    score_blocks = soup.find_all("div")

    for div in score_blocks:
        txt = div.get_text(" ", strip=True)

        if "Overs" in txt and "/" in txt:
            innings.append({
                "innings_name": txt,
                "score": "",
                "wickets": "",
                "overs": "",
            })

    return {
        "match_id": match_id,
        "title": soup.title.text if soup.title else "",
        "innings": innings
    }


async def get_squads(match_id):
    cache_key = f"squad_{match_id}"

    cache = get_cache(cache_key, ttl=86400)

    if cache:
        return cache

    url = f"{BASE}/cricket-match-squads/{match_id}"
    html = await fetch(url)

    soup = BeautifulSoup(html, "lxml")

    teams = []

    headers = soup.find_all(["h2", "h3"])

    for h in headers:
        team_name = h.get_text(strip=True)

        ul = h.find_next("ul")

        if not ul:
            continue

        players = []

        for li in ul.find_all("li"):
            players.append({
                "name": li.get_text(strip=True),
                "role": None
            })

        teams.append({
            "team_name": team_name,
            "players": players
        })

    result = {
        "match_id": match_id,
        "teams": teams
    }

    set_cache(cache_key, result)

    return result