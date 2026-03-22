
# run_emergency_help.py
from typing import List, Dict, Optional
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from emergency_help_tool import find_emergency_help_points

# ------------------ Schemas for Swagger ------------------

class HelpPoint(BaseModel):
    name: str
    address: str
    lat: float
    lon: float
    distance_m: int = Field(..., ge=0)
    type: str
    icon: str
    priority: int
    maps_link: str

class GroupedHelpPoints(BaseModel):
    mrt: List[HelpPoint] = []
    polyclinic: List[HelpPoint] = []
    hospital: List[HelpPoint] = []
    police: List[HelpPoint] = []

class HelpPointsResponse(BaseModel):
    ok: bool = True
    search_radius_m: int
    help_points: GroupedHelpPoints
    emergency_message: str
    emergency_numbers: Dict[str, str]

# ------------------ FastAPI app ------------------

app = FastAPI(title="Emergency Help API (grouped output)")

@app.get("/", summary="Health check")
def root():
    return {"message": "Emergency Help API running", "docs": "/docs"}

@app.get(
    "/api/emergency/help-points",
    response_model=HelpPointsResponse,
    summary="Find nearby MRT, polyclinics, hospitals, police",
    description="Returns grouped help points in priority order: MRT → Polyclinic → Hospital → Police.",
)
def api_help_points(
    lat: float = Query(..., description="Current latitude"),
    lon: float = Query(..., description="Current longitude"),
    radius_m: int = Query(2000, ge=100, le=10000, description="Search radius in meters"),
    per_type_limit: int = Query(3, ge=1, le=10, description="Max results per category"),
):
    raw = find_emergency_help_points(lat=lat, lon=lon, radius_m=radius_m, per_type_limit=per_type_limit)

    # Map dict -> Pydantic models for clean docs
    grouped = GroupedHelpPoints(
        mrt=[HelpPoint(**hp) for hp in raw["help_points"].get("mrt", [])],
        polyclinic=[HelpPoint(**hp) for hp in raw["help_points"].get("polyclinic", [])],
        hospital=[HelpPoint(**hp) for hp in raw["help_points"].get("hospital", [])],
        police=[HelpPoint(**hp) for hp in raw["help_points"].get("police", [])],
    )

    return HelpPointsResponse(
        ok=raw["ok"],
        search_radius_m=raw["search_radius_m"],
        help_points=grouped,
        emergency_message=raw["emergency_message"],
        emergency_numbers=raw["emergency_numbers"],
    )
