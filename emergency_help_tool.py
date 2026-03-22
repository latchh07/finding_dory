# emergency_help_tool.py
"""
Minimal Emergency Help Point tool (grouped output)
"""

from math import radians, sin, cos, asin, sqrt
from typing import List, Dict, Any
import requests

__all__ = ["find_emergency_help_points"]

# Priority order: MRT > Polyclinic > Hospital > Police
HELP_TYPES = [
    {"search": "MRT",        "icon": "transport", "priority": 1},
    {"search": "polyclinic", "icon": "medical",   "priority": 2},
    {"search": "hospital",   "icon": "hospital",  "priority": 3},
    {"search": "police",     "icon": "police",    "priority": 4},
]

ONEMAP_URL = "https://www.onemap.gov.sg/api/common/elastic/search"

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = (sin(dphi/2)**2) + cos(p1) * cos(p2) * (sin(dl/2)**2)
    return 2 * R * asin(sqrt(a))

def _query_onemap(term: str, page: int = 1, timeout: int = 10) -> List[dict]:
    try:
        r = requests.get(
            ONEMAP_URL,
            params={"searchVal": term, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": page},
            timeout=timeout
        )
        r.raise_for_status()
        return r.json().get("results", []) or []
    except Exception:
        return []

def find_emergency_help_points(
    lat: float,
    lon: float,
    radius_m: int = 2000,
    per_type_limit: int = 3,
    dedupe_distance_m: int = 40,
) -> Dict[str, Any]:
    """
    Look up nearby MRT, polyclinics, hospitals, police.
    Groups results by category.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for t in HELP_TYPES:
        results = _query_onemap(t["search"])
        collected: List[Dict[str, Any]] = []
        for res in results:
            try:
                pt_lat = float(res["LATITUDE"])
                pt_lon = float(res["LONGITUDE"])
            except Exception:
                continue

            dist = int(_haversine_m(lat, lon, pt_lat, pt_lon))
            if dist > radius_m:
                continue

            collected.append({
                "name": (res.get("SEARCHVAL") or "").strip(),
                "address": (res.get("ADDRESS") or "").strip(),
                "lat": pt_lat,
                "lon": pt_lon,
                "distance_m": dist,
                "type": t["search"],
                "icon": t["icon"],
                "priority": t["priority"],
                "maps_link": f"https://www.onemap.gov.sg/v2/?lat={pt_lat}&lng={pt_lon}&zoom=17",
            })

        # sort by distance & limit
        collected.sort(key=lambda x: x["distance_m"])
        grouped[t["search"].lower()] = collected[:per_type_limit]

    return {
        "ok": True,
        "search_radius_m": radius_m,
        "help_points": grouped,
        "emergency_message": "Choose the nearest safe place. If urgent, call 995 (ambulance) or 999 (police).",
        "emergency_numbers": {"ambulance": "995", "police": "999", "fire": "995"},
    }

