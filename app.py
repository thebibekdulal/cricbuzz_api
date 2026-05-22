from fastapi import FastAPI
from scraper import (
    get_live_matches,
    get_upcoming_matches,
    get_recent_matches,
    get_scorecard,
    get_squads,
)

app = FastAPI(
    title="Cricbuzz Live Score API",
    version="1.0.0"
)


@app.get("/live")
async def live_matches():
    return await get_live_matches()


@app.get("/upcoming")
async def upcoming_matches():
    return await get_upcoming_matches()


@app.get("/recent")
async def recent_matches():
    return await get_recent_matches()


@app.get("/scorecard/{match_id}")
async def scorecard(match_id: str):
    return await get_scorecard(match_id)


@app.get("/squads/{match_id}")
async def squads(match_id: str):
    return await get_squads(match_id)