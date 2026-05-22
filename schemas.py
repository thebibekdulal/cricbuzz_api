from pydantic import BaseModel
from typing import List, Optional


class MatchBase(BaseModel):
    match_id: str
    title: str
    team1: str
    team2: str
    status: str
    match_url: str


class LiveMatch(MatchBase):
    score: Optional[str] = None
    overs: Optional[str] = None


class UpcomingMatch(MatchBase):
    start_time: Optional[str] = None


class RecentMatch(MatchBase):
    result: Optional[str] = None


class ScorecardInnings(BaseModel):
    innings_name: str
    score: str
    wickets: str
    overs: str


class ScorecardResponse(BaseModel):
    match_id: str
    title: str
    innings: List[ScorecardInnings]


class Player(BaseModel):
    name: str
    role: Optional[str] = None


class SquadTeam(BaseModel):
    team_name: str
    players: List[Player]


class SquadResponse(BaseModel):
    match_id: str
    teams: List[SquadTeam]