import os
import time
import json
import base64
import requests
import polyline
import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter  
from math import radians, sin, cos, asin, sqrt
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict 
from dataclasses import dataclass
from enum import Enum
from fastapi import FastAPI, APIRouter, Query
from fastapi.responses import JSONResponse
from med_notification_tool import router as meds_router
from memory_notification_tool import router as memory_router
from fastapi import APIRouter, Query
from llm_router import generate_text, choose_models
from agent_runner import router as agent_router 
# app.py (top-level imports)
from fastapi import APIRouter, Body, Query
from contact_tools import (
    add_contact, list_contacts, set_primary_contact, get_primary_contact,
    update_last_location, build_emergency_payload,
)
import re

# ========================================================
# AWS Configuration
# ========================================================

REGION = "us-east-1"
BUCKET = "findingdoryuserdata"
s3 = boto3.client("s3", region_name=REGION)

# ========================================================
# Data Models - Core structures (frequent_places only)
# ========================================================

class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    URGENT = "urgent"

@dataclass
class SafetyAlert:
    user_id: int
    alert_type: str
    level: AlertLevel
    message: str
    location: Tuple[float, float]
    timestamp: datetime
    acknowledged: bool = False
    caregiver_notified: bool = False

@dataclass
class FrequentPlace:
    """Canonical place model"""
    name: str
    address: str
    lat: float
    lon: float
    category: str = "other"           # optional semantics
    visit_frequency: str = "regular"  # optional semantics
    notes: Optional[str] = None

@dataclass
class UserProfile:
    user_id: int
    name: str
    home_location: Tuple[float, float]
    frequent_places: List[FrequentPlace]
    emergency_contacts: List[dict]
    medical_info: dict
    safety_preferences: dict

# ========================================================
# S3 Data Access
# ========================================================

def get_user_json(user_id: int) -> dict:
    """
    Retrieve a user's profile JSON from S3.
    Destination storage is removed; only frequent_places is canonical.
    """
    key = f"users/{user_id}.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            # Default skeleton structure
            return {
                "user_id": user_id,
                "name": f"User {user_id}",
                "home_location": [1.3521, 103.8198],  # Singapore
                "frequent_places": [],
                "emergency_contacts": [],
                "medical_info": {},
                "safety_preferences": {"geofence_radius": 150, "check_interval": 300}
            }
        raise

def put_user_json(user_id: int, data: dict):
    key = f"users/{user_id}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json"
    )
    print(f"✅ S3 updated: {key}")

def get_user_profile(user_id: int) -> UserProfile:
    """
    Convert S3 JSON data to UserProfile (frequent_places only).
    """
    data = get_user_json(user_id)

    places: List[FrequentPlace] = []
    for p in data.get("frequent_places", []):
        try:
            places.append(FrequentPlace(
                name=p.get("name", ""),
                address=p.get("address", "") or "",
                lat=float(p.get("lat", 0.0)),
                lon=float(p.get("lon", 0.0)),
                category=p.get("category", "other"),
                visit_frequency=p.get("visit_frequency", "regular"),
                notes=p.get("notes")
            ))
        except Exception:
            continue

    return UserProfile(
        user_id=data["user_id"],
        name=data.get("name", f"User {user_id}"),
        home_location=tuple(data.get("home_location", [1.3521, 103.8198])),
        frequent_places=places,
        emergency_contacts=data.get("emergency_contacts", []),
        medical_info=data.get("medical_info", {}),
        safety_preferences=data.get("safety_preferences", {"geofence_radius": 150, "check_interval": 300})
    )

# ========================================================
# Geocoding Helpers (Google primary, OneMap fallback)
# ========================================================

def geocode_google_maps(address: str) -> Optional[Tuple[float, float]]:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("❌ GOOGLE_MAPS_API_KEY not set")
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": api_key, "region": "sg"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            print("Geocode error:", data.get("status"), data.get("error_message"))
            return None
        loc = data["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])
    except Exception as e:
        print("Google geocode error:", e)
        return None

def geocode_onemap_fallback(address: str) -> Optional[Tuple[float, float]]:
    try:
        url = "https://www.onemap.gov.sg/api/common/elastic/search"
        params = {"searchVal": address, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("results"):
            r = data["results"][0]
            return float(r["LATITUDE"]), float(r["LONGITUDE"])
    except Exception as e:
        print(f"OneMap geocoding error: {e}")
    return None

def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    coords = geocode_google_maps(address)
    if coords:
        return coords
    return geocode_onemap_fallback(address)

def reverse_geocode_google(lat: float, lon: float) -> Optional[str]:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return None
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        resp = requests.get(url, params={"latlng": f"{lat},{lon}", "key": api_key, "region": "sg"}, timeout=10)
        data = resp.json()
        if data.get("status") == "OK" and data.get("results"):
            return data["results"][0]["formatted_address"]
    except Exception:
        pass
    return None

# ========================================================
# Utility Functions - Distance & HTML stripping
# ========================================================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = (sin(dphi / 2) ** 2) + cos(p1) * cos(p2) * (sin(dl / 2) ** 2)
    return 2 * R * asin(sqrt(a))

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_m(lat1, lon1, lat2, lon2) / 1000.0

TAG_RE = re.compile(r"<[^>]+>")

# ========================================================
# Google Maps API Integration (primary)
# ========================================================

def get_google_maps_route(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    mode: str = "walking"
) -> Dict[str, any]:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return {"success": False, "error": "GOOGLE_MAPS_API_KEY environment variable not set"}

    mode_mapping = {"walk": "walking", "drive": "driving", "cycle": "bicycling", "pt": "transit"}
    google_mode = mode_mapping.get(mode, mode)

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{start_lat},{start_lon}",
        "destination": f"{end_lat},{end_lon}",
        "mode": google_mode,
        "key": api_key,
        "alternatives": "false",
        "units": "metric",
        "language": "en"
    }

    try:
        print(f"[Google Maps] Requesting route: {start_lat},{start_lon} -> {end_lat},{end_lon} ({google_mode})")
        response = requests.get(url, params=params, timeout=30)
        data = response.json()

        if response.status_code != 200:
            return {"success": False, "error": f"HTTP {response.status_code}: {data.get('error_message', 'Unknown error')}"}

        if data.get("status") != "OK" or not data.get("routes"):
            return {"success": False, "error": f"Google Maps API error: {data.get('status')} - {data.get('error_message', 'No details')}"}

        route = data["routes"][0]
        leg = route["legs"][0]

        # Decode polyline
        encoded_polyline = route["overview_polyline"]["points"]
        decoded_points = polyline.decode(encoded_polyline)

        # Steps
        steps = []
        for step in leg.get("steps", []):
            instruction = TAG_RE.sub("", step.get("html_instructions", "")).strip()
            steps.append({
                "instruction": instruction,
                "distance_m": step["distance"]["value"],
                "duration_s": step["duration"]["value"],
                "start_location": step.get("start_location", {}),
                "end_location": step.get("end_location", {})
            })

        print(f"[Google Maps] SUCCESS - Duration: {leg['duration']['value']}s, Distance: {leg['distance']['value']}m")

        return {
            "success": True,
            "data": {
                "duration_s": leg["duration"]["value"],
                "distance_m": leg["distance"]["value"],
                "polyline_points": decoded_points,
                "start_address": leg.get("start_address"),
                "end_address": leg.get("end_address"),
                "steps": steps,
                "source": "google_maps"
            }
        }

    except Exception as e:
        print(f"[Google Maps] ERROR: {str(e)}")
        return {"success": False, "error": f"Request failed: {str(e)}"}

# ========================================================
# OneMap Routing Fallback
# ========================================================

def _jwt_expiry_epoch(token: str) -> Optional[int]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None

_ONEMAP_TOKEN_CACHE = {"token": None, "expiry_epoch": 0}

def _fetch_onemap_token(email: str, password: str) -> Optional[str]:
    auth_urls = [
        "https://www.onemap.gov.sg/api/auth/post/getToken",
        "https://developers.onemap.sg/privateapi/auth/post/getToken"
    ]
    for url in auth_urls:
        try:
            r = requests.post(url, json={"email": email, "password": password},
                              headers={"Content-Type": "application/json"}, timeout=10)
            if r.status_code == 200:
                return r.json().get("access_token")
        except Exception:
            continue
    return None

def _get_onemap_token() -> Optional[str]:
    now = int(time.time())
    if _ONEMAP_TOKEN_CACHE["token"] and _ONEMAP_TOKEN_CACHE["expiry_epoch"] > now:
        return _ONEMAP_TOKEN_CACHE["token"]
    env_token = os.getenv("ONEMAP_TOKEN")
    if env_token:
        exp = _jwt_expiry_epoch(env_token)
        _ONEMAP_TOKEN_CACHE["token"] = env_token
        _ONEMAP_TOKEN_CACHE["expiry_epoch"] = exp if exp else (now + 24 * 3600)
        return env_token
    email = os.getenv("ONEMAP_EMAIL")
    password = os.getenv("ONEMAP_PASSWORD")
    if email and password:
        token = _fetch_onemap_token(email, password)
        if token:
            _ONEMAP_TOKEN_CACHE["token"] = token
            _ONEMAP_TOKEN_CACHE["expiry_epoch"] = now + 259200  # 3 days
            return token
    return None

def get_onemap_route_fallback(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    route_type: str = "walk"
) -> Dict[str, any]:
    token = _get_onemap_token()
    current_time = time.strftime("%H:%M:%S")
    current_date = time.strftime("%m-%d-%Y")

    endpoints = [{
        "name": "Public API",
        "url": "https://www.onemap.gov.sg/api/public/routingsvc/route",
        "params": {
            "start": f"{start_lat},{start_lon}",
            "end": f"{end_lat},{end_lon}",
            "routeType": route_type,
            "date": current_date,
            "time": current_time,
        },
        "headers": {"Accept": "application/json", "User-Agent": "finding-dory/3.0"}
    }]

    if token:
        endpoints.append({
            "name": "Private API",
            "url": "https://www.onemap.gov.sg/api/private/routingsvc/route",
            "params": {
                "start": f"{start_lat},{start_lon}",
                "end": f"{end_lat},{end_lon}",
                "routeType": route_type,
                "token": token,
                "date": current_date,
                "time": current_time,
            },
            "headers": {"Accept": "application/json", "User-Agent": "finding-dory/3.0"}
        })

    for ep in endpoints:
        try:
            print(f"[OneMap Fallback] Trying {ep['name']}")
            r = requests.get(ep["url"], params=ep["params"], headers=ep["headers"], timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == 0 and data.get("route_geometry"):
                    poly_points = polyline.decode(data["route_geometry"])
                    summary = data.get("route_summary", {})
                    return {
                        "success": True,
                        "data": {
                            "duration_s": int(summary.get("total_time", 0)),
                            "distance_m": int(summary.get("total_distance", 0)),
                            "polyline_points": poly_points,
                            "source": "onemap",
                            "endpoint": ep["name"]
                        }
                    }
        except Exception as e:
            print(f"[OneMap Fallback] Error with {ep['name']}: {e}")
            continue

    return {"success": False, "error": "OneMap routing failed - all endpoints exhausted"}

# ========================================================
# Hybrid Routing
# ========================================================

def get_hybrid_route(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    route_type: str = "walk"
) -> Dict[str, any]:
    print(f"[Hybrid Routing] Route request: {start_lat},{start_lon} -> {end_lat},{end_lon} ({route_type})")
    google_mode_map = {"walk": "walking", "drive": "driving", "cycle": "bicycling", "pt": "transit"}
    google_mode = google_mode_map.get(route_type, "walking")

    print("[Hybrid] Attempting Google Maps (primary)...")
    google_result = get_google_maps_route(start_lat, start_lon, end_lat, end_lon, google_mode)
    if google_result["success"]:
        print("[Hybrid] SUCCESS with Google Maps")
        return google_result

    print(f"[Hybrid] Google Maps failed ({google_result['error']}), trying OneMap fallback...")
    onemap_result = get_onemap_route_fallback(start_lat, start_lon, end_lat, end_lon, route_type)
    if onemap_result["success"]:
        print("[Hybrid] SUCCESS with OneMap fallback")
        return onemap_result

    return {"success": False, "google_error": google_result["error"], "onemap_error": onemap_result["error"]}

# ========================================================
# In-memory session storage
# ========================================================

_ALERT_HISTORY: List[SafetyAlert] = []
_LOCATION_HISTORY: Dict[int, List[dict]] = {}
_recent: Dict[int, List[Tuple[float, float]]] = {}

# ========================================================
# Safety Analysis (uses frequent_places only)
# ========================================================

def left_home(lat: float, lng: float, home_lat: float, home_lng: float, radius_m: int = 120) -> bool:
    return haversine_m(lat, lng, home_lat, home_lng) > radius_m

def unusual_route(recent_points: List[Tuple[float, float]], frequent_places: List[Tuple[float, float]], near_km: float = 0.6) -> bool:
    if not recent_points:
        return False
    last_lat, last_lng = recent_points[-1]
    return all(haversine_km(last_lat, last_lng, fp[0], fp[1]) > near_km for fp in frequent_places)

def analyze_movement_pattern(user_id: int, current_location: Tuple[float, float]) -> List[SafetyAlert]:
    alerts = []
    history = _LOCATION_HISTORY.get(user_id, [])
    if len(history) < 5:
        return alerts

    current_time = datetime.now()

    # Repetitive circling pattern (lingering)
    recent_points = history[-10:]
    if len(recent_points) >= 6:
        center_lat = sum(p['lat'] for p in recent_points) / len(recent_points)
        center_lon = sum(p['lng'] for p in recent_points) / len(recent_points)
        within_radius = all(haversine_m(p['lat'], p['lng'], center_lat, center_lon) < 50 for p in recent_points)
        time_span = (recent_points[-1]['timestamp'] - recent_points[0]['timestamp']).total_seconds()

        if within_radius and time_span > 600:
            alerts.append(SafetyAlert(
                user_id=user_id,
                alert_type="possible_confusion",
                level=AlertLevel.WARNING,
                message="User may be confused - staying in small area for extended time",
                location=current_location,
                timestamp=current_time
            ))
    return alerts

def check_time_based_concerns(user_id: int, current_location: Tuple[float, float]) -> List[SafetyAlert]:
    alerts = []
    profile = get_user_profile(user_id)
    current_time = datetime.now()
    current_hour = current_time.hour

    # Night wandering (22:00-06:00)
    if 22 <= current_hour or current_hour <= 6:
        home_lat, home_lon = profile.home_location
        distance_from_home = haversine_m(current_location[0], current_location[1], home_lat, home_lon)
        if distance_from_home > profile.safety_preferences.get('geofence_radius', 150):
            alerts.append(SafetyAlert(
                user_id=user_id,
                alert_type="night_wandering",
                level=AlertLevel.URGENT,
                message=f"User outside home area at night ({current_time.strftime('%H:%M')})",
                location=current_location,
                timestamp=current_time
            ))
    return alerts

def generate_recommendations(user_id: int, location: Tuple[float, float], alerts: List[str]) -> List[str]:
    recommendations = []
    current_hour = datetime.now().hour

    if "left_home" in alerts:
        if 8 <= current_hour <= 18:
            recommendations.extend(["Remember to carry your house keys and phone", "Let someone know where you're going"])
        else:
            recommendations.extend(["Consider returning home - it's getting late", "Make sure you have good lighting and transport"])

    if "unusual_route" in alerts:
        recommendations.extend(["This area seems unfamiliar - stay on main roads", "Look for landmarks you recognize"])

    return recommendations

# ========================================================
# FastAPI app
# ========================================================

app = FastAPI(
    title="Finding/Guiding Dory - Dementia Helper Backend",
    description="Safety & navigation system with Google Maps (primary) and OneMap fallback. Canonical store: frequent_places.",
    version="4.0.0"
)

# ---- Contacts API ----
contacts_router = APIRouter(prefix="/api/contacts", tags=["contacts"])

@contacts_router.post("/add")
def api_add_contact(payload: dict = Body(...)):
    # expected: {user_id, name, phone, relation?, type?, primary?, notify_via?, notes?}
    return add_contact(**payload)

@contacts_router.get("/list")
def api_list_contacts(
    user_id: int = Query(...),
    kind: str | None = Query(None),
    relation: str | None = Query(None),
    primary: bool | None = Query(None),
):
    return {"ok": True, "contacts": list_contacts(user_id=user_id, kind=kind, relation=relation, primary=primary)}

@contacts_router.post("/set-primary")
def api_set_primary(
    user_id: int = Query(...),
    contact_id: str | None = Query(None),
    phone: str | None = Query(None),
):
    return set_primary_contact(user_id=user_id, contact_id=contact_id, phone=phone)

@contacts_router.get("/primary")
def api_get_primary(user_id: int = Query(...)):
    return {"ok": True, "contact": get_primary_contact(user_id)}

@contacts_router.post("/update-last-location")
def api_update_last_location(payload: dict = Body(...)):
    # expected: {user_id, lat, lng, timestamp_iso?}
    return update_last_location(**payload)

@contacts_router.get("/emergency-payload")
def api_emergency_payload(
    user_id: int = Query(...),
    current_lat: float | None = Query(None),
    current_lng: float | None = Query(None),
    address: str | None = Query(None),
    contact_id: str | None = Query(None),
    phone_override: str | None = Query(None),
    message_prefix: str = Query("EMERGENCY: I need help."),
):
    return build_emergency_payload(
        user_id=user_id,
        current_lat=current_lat,
        current_lng=current_lng,
        address=address,
        contact_id=contact_id,
        phone_override=phone_override,
        message_prefix=message_prefix,
    )

@app.get("/")
def home():
    return {
        "message": "Dementia Helper Backend running",
        "status": "healthy",
        "data_storage": "AWS S3",
        "features": [
            "Location tracking with geofencing",
            "Frequent places management (canonical store)",
            "Emergency help point finder",
            "Safety alerts and recommendations",
            "Hybrid routing (Google primary, OneMap fallback)"
        ],
        "key_endpoints": {
            "location_tracking": "/location/ping/enhanced",
            "frequent_places_add": "/api/frequent-places/add",
            "frequent_places_list": "/api/destinations/list",
            "route_by_name": "/places/route-by-name",
            "navigation_start": "/api/destinations/start-navigation",
            "emergency": "/api/emergency/help-points",
            "routing": "/api/route"
        }
    }

@app.get("/api/destinations/where-going") 
def where_are_you_going_prompt(user_id: int = Query(1)):
    profile = get_user_profile(user_id)
    # exclude "home"
    destinations = [p for p in profile.frequent_places if (p.category or "other") != "home"]
    frequency_order = {"daily": 1, "weekly": 2, "monthly": 3, "regular": 4}
    destinations.sort(key=lambda x: frequency_order.get(getattr(x, "visit_frequency", "regular"), 4))
    return {
        "ok": True,
        "message": f"Hi {profile.name}! Where are you going today?",
        "destinations": [
            {
                "name": p.name,
                "category": p.category,
                "address": p.address,
                "notes": p.notes,
                "visit_frequency": p.visit_frequency,
                "display_text": f"{p.name}" + (f" ({p.notes})" if p.notes else "")
            }
            for p in destinations
        ],
        "quick_options": [
            {"text": "Just going for a walk", "type": "casual"},
            {"text": "Running errands nearby", "type": "general"},
            {"text": "Meeting someone", "type": "social"},
            {"text": "Other destination", "type": "other"}
        ]
    }

@app.post("/api/destinations/start-navigation")
def start_navigation_to_destination(
    user_id: int = Query(1),
    destination_name: str = Query(..., description="Name of frequent place"),
    current_lat: float = Query(..., description="Current latitude"),
    current_lng: float = Query(..., description="Current longitude")
):
    profile = get_user_profile(user_id)
    # find by frequent_places
    dest = next((p for p in profile.frequent_places if p.name.lower() == destination_name.lower()), None)
    if not dest:
        return {"ok": False, "error": f"Destination '{destination_name}' not found"}

    result = get_hybrid_route(current_lat, current_lng, dest.lat, dest.lon, "walk")
    if not result["success"]:
        total_distance_m = int(haversine_m(current_lat, current_lng, dest.lat, dest.lon))
        estimated_minutes = int(total_distance_m / 1000 * 12)
        return {
            "ok": True,
            "navigation": {
                "destination": {
                    "name": dest.name,
                    "address": dest.address,
                    "category": dest.category,
                    "notes": dest.notes
                },
                "route_info": {
                    "total_distance_m": total_distance_m,
                    "estimated_minutes": estimated_minutes,
                    "estimated_time": f"About {estimated_minutes} minutes walking"
                },
                "text_directions": [
                    f"Walk approximately {total_distance_m}m to reach {dest.name}",
                    "Head towards the destination address shown",
                    "Use familiar roads and landmarks"
                ],
                "summary": f"Walk {total_distance_m}m to reach {dest.name}",
                "routing_available": False
            },
            "safety_reminders": [
                "Stay on main paths and roads",
                "Look for familiar landmarks",
                (f"If you get confused, call {profile.emergency_contacts[0]['name']} at {profile.emergency_contacts[0]['phone']}"
                 if profile.emergency_contacts else "If you get confused, ask for help"),
                "Take your time and stay safe"
            ]
        }

    data = result["data"]
    total_distance_m = data.get("distance_m", 0)
    estimated_minutes = int(total_distance_m / 1000 * 12)
    text_directions = []
    if data.get("steps"):
        text_directions = [f"Step {i+1}: {s['instruction']} (about {s.get('distance_m', 0)}m)" for i, s in enumerate(data["steps"])]

    return {
        "ok": True,
        "navigation": {
            "destination": {
                "name": dest.name,
                "address": dest.address,
                "category": dest.category,
                "notes": dest.notes
            },
            "route_info": {
                "total_distance_m": total_distance_m,
                "estimated_minutes": estimated_minutes,
                "estimated_time": f"About {estimated_minutes} minutes walking"
            },
            "text_directions": text_directions,
            "summary": f"Walk {total_distance_m//100 * 100}m to reach {dest.name}",
            "routing_available": True,
            "source": data.get("source", "unknown")
        },
        "safety_reminders": [
            "Stay on main paths and roads",
            "Look for familiar landmarks",
            (f"If you get confused, call {profile.emergency_contacts[0]['name']} at {profile.emergency_contacts[0]['phone']}"
             if profile.emergency_contacts else "If you get confused, ask for help"),
            "Take your time and stay safe"
        ]
    }

# ========================================================
# Location Tracking API
# ========================================================

router = APIRouter(prefix="/location", tags=["location"])

@router.post("/ping/enhanced")
def enhanced_ping(
    user_id: int = Query(1),
    lat: float = Query(...),
    lng: float = Query(...),
    battery_level: Optional[int] = Query(None, ge=0, le=100),
    connection_quality: Optional[str] = Query(None)
):
    current_time = datetime.now()
    current_location = (lat, lng)

    # Store location
    if user_id not in _LOCATION_HISTORY:
        _LOCATION_HISTORY[user_id] = []
    _LOCATION_HISTORY[user_id].append({
        "lat": lat, "lng": lng, "timestamp": current_time, 
        "battery_level": battery_level, "connection_quality": connection_quality
    })
    _LOCATION_HISTORY[user_id] = _LOCATION_HISTORY[user_id][-50:]

    # Update recents
    if user_id not in _recent:
        _recent[user_id] = []
    _recent[user_id].append((lat, lng))
    _recent[user_id] = _recent[user_id][-10:]

    profile = get_user_profile(user_id)

    # Basic checks
    basic_alerts = []
    home_lat, home_lng = profile.home_location
    geofence_radius = profile.safety_preferences.get('geofence_radius', 150)
    just_left_home = left_home(lat, lng, home_lat, home_lng, radius_m=geofence_radius)
    if just_left_home:
        basic_alerts.append("left_home")

    # Unusual route vs frequent_places
    saved_locations = [(p.lat, p.lon) for p in profile.frequent_places]
    if unusual_route([(lat, lng)], saved_locations):
        basic_alerts.append("unusual_route")

    # Advanced analysis
    movement_alerts = analyze_movement_pattern(user_id, current_location)
    time_alerts = check_time_based_concerns(user_id, current_location)

    for alert in movement_alerts + time_alerts:
        _ALERT_HISTORY.append(alert)

    # Battery alert
    battery_alert = None
    if battery_level is not None and battery_level < 20:
        battery_alert = {"type": "low_battery", "level": battery_level, "message": f"Phone battery at {battery_level}% - find a charger soon"}

    # Destination prompt when leaving home (still useful UX)
    destination_prompt = None
    if just_left_home and len(_LOCATION_HISTORY[user_id]) <= 3:
        destination_prompt = {
            "should_ask": True,
            "message": f"Hi {profile.name}! I noticed you've left home. Where are you going today?",
            "prompt_endpoint": "/api/destinations/where-going"
        }

    return {
        "ok": True,
        "basic_alerts": basic_alerts,
        "advanced_alerts": [
            {
                "type": alert.alert_type,
                "level": alert.level.value,
                "message": alert.message,
                "timestamp": alert.timestamp.isoformat()
            }
            for alert in movement_alerts + time_alerts
        ],
        "battery_alert": battery_alert,
        "destination_prompt": destination_prompt,
        "user_status": {
            "name": profile.name,
            "distance_from_home_m": int(haversine_m(lat, lng, home_lat, home_lng)),
            "last_update": current_time.isoformat()
        },
        "recommendations": generate_recommendations(user_id, current_location, basic_alerts)
    }

app.include_router(router)

# ========================================================
# Places router: route-by-name backed by frequent_places
# ========================================================

places_router = APIRouter(prefix="/places", tags=["places"])

@places_router.get("/route-by-name")
def route_to_place_by_name(
    user_id: int = Query(1),
    name: str = Query(...),
    route_type: str = Query("walk", description="walk | drive | cycle | pt"),
):
    profile = get_user_profile(user_id)
    search_name = name.strip().lower()

    # Find in frequent_places
    destination = next((p for p in profile.frequent_places
                        if (search_name in p.name.lower()) or (p.name.lower() in search_name)
                        or (search_name in (p.category or "").lower())), None)

    if not destination:
        return JSONResponse({"ok": False, "error": f"Place '{name}' not found in frequent places"}, status_code=404)

    end_lat, end_lon = destination.lat, destination.lon

    # Start = most recent ping, else home
    recents = _recent.get(user_id, [])
    if recents:
        start_lat, start_lon = recents[-1]
    else:
        start_lat, start_lon = profile.home_location

    result = get_hybrid_route(start_lat, start_lon, end_lat, end_lon, route_type)
    if not result["success"]:
        return JSONResponse({
            "ok": False,
            "error": "All routing services failed",
            "google_error": result.get("google_error"),
            "onemap_error": result.get("onemap_error")
        }, status_code=502)

    data = result["data"]
    return {
        "ok": True,
        "name": destination.name,
        "start": {"lat": start_lat, "lon": start_lon},
        "end": {"lat": end_lat, "lon": end_lon},
        "summary": {"total_time_s": data.get("duration_s", 0), "total_distance_m": data.get("distance_m", 0)},
        "polyline": [[lat, lon] for (lat, lon) in data.get("polyline_points", [])],
        "route_type": route_type,
        "source": data.get("source", "unknown"),
        "addresses": {"start": data.get("start_address"), "end": data.get("end_address")} if data.get("source") == "google_maps" else None
    }

app.include_router(places_router)
app.include_router(meds_router)      # /api/meds  (medication reminders)
app.include_router(memory_router)    # /api/memory (memory celebration payloads)
app.include_router(contacts_router)  # /api/contacts (emergency contacts)

# --- Mount Streamlit notifications backend into main app & start its scheduler ---
try:
    from med_notification_tool import router as notif_router, scheduler as notif_scheduler
    app.include_router(notif_router)

    @app.on_event("startup")
    def _start_notif_scheduler():
        if not notif_scheduler.running:
            notif_scheduler.start()
except Exception as e:
    print(f"[notifications_streamlit] not mounted: {e}")

# --- Emergency: help points ---
from emergency_help_tool import find_emergency_help_points
emerg_router = APIRouter(prefix="/api/emergency", tags=["emergency"])

@emerg_router.get("/help-points")
def emergency_help_points(lat: float = Query(...), lon: float = Query(...), radius_m: int = Query(2000), per_type_limit: int = Query(3)):
    return find_emergency_help_points(lat=lat, lon=lon, radius_m=radius_m, per_type_limit=per_type_limit)

app.include_router(emerg_router)

# --- Destinations: list/add/search via frequent_places ---
dest_router = APIRouter(prefix="/api/destinations", tags=["destinations"])

@dest_router.get("/list")
def list_destinations(user_id: int = Query(1)):
    profile = get_user_profile(user_id)
    return {"ok": True, "frequent_places": [p.__dict__ for p in profile.frequent_places]}

@dest_router.post("/add")
def add_destination(
    user_id: int = Query(1),
    name: str = Query(...),
    address: str = Query(...),
    category: str = Query("other"),
    visit_frequency: str = Query("regular"),
    notes: str = Query(None),
):
    data = get_user_json(user_id)
    coords = geocode_address(address)
    if not coords:
        return JSONResponse({"ok": False, "error": "geocoding_failed"}, status_code=400)
    lat, lon = coords
    fp = {
        "name": name, "address": address, "lat": lat, "lon": lon,
        "category": category, "visit_frequency": visit_frequency, "notes": notes,
        "added_at": datetime.utcnow().isoformat() + "Z",
    }
    data.setdefault("frequent_places", []).append(fp)
    put_user_json(user_id, data)
    return {"ok": True, "place": fp}

@dest_router.get("/search")
def search_destinations(user_id: int = Query(1), query: str = Query(...)):
    profile = get_user_profile(user_id)
    q = (query or "").lower()
    matches = [p.__dict__ for p in profile.frequent_places if q in p.name.lower() or q in (p.category or "").lower() or q in (p.address or "").lower()]
    return {"ok": True, "matches": matches, "count": len(matches)}

app.include_router(dest_router)

# --- Safety checklist (simple canned) ---
safety_router = APIRouter(prefix="/api", tags=["safety"])

@safety_router.post("/safety-check")
def safety_check(user_id: int = Query(1), check_type: str = Query("leaving_home")):
    checklists = {
        "leaving_home": {
            "title": "Before you go",
            "message": "A few quick checks for a safe trip:",
            "items": ["Phone charged", "Keys with you", "Wallet/ID", "Tell someone where you’re going"],
        },
        "emergency": {
            "title": "Stay calm — help steps",
            "message": "Head to the nearest safe place. If urgent, call 995.",
            "items": ["Move to a well-lit area", "Ask nearby staff for help", "Call an emergency contact"],
        },
        "routine": {
            "title": "Daily check",
            "message": "A gentle reminder to keep the day on track:",
            "items": ["Take medications", "Drink water", "Plan a simple activity"],
        },
    }
    payload = checklists.get(check_type, checklists["routine"])
    return {"ok": True, "checklist": payload}

app.include_router(safety_router)

# ---- LLM test route (for /docs) ----
llm_test_router = APIRouter(prefix="/api/llm", tags=["llm"])

app.include_router(agent_router) 

@llm_test_router.get("/test")
def llm_test(
    prompt: str = Query("Summarize in one short sentence."),
    task: str = Query("summarize"),
    max_tokens: int = Query(80),
    temperature: float = Query(0.2),
):
    out = generate_text(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        prefer=choose_models(task),
    )
    return {"model_used": out["model_id"], "text": out["text"], "raw": out["raw"]}

app.include_router(llm_test_router)

# ========================================================
# Alerts API
# ========================================================

@app.get("/api/alerts/{user_id}")
def get_user_alerts(
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    unacknowledged_only: bool = Query(False)
):
    user_alerts = [alert for alert in _ALERT_HISTORY if alert.user_id == user_id]
    if unacknowledged_only:
        user_alerts = [alert for alert in user_alerts if not alert.acknowledged]
    user_alerts.sort(key=lambda x: x.timestamp, reverse=True)
    return {
        "ok": True,
        "alerts": [
            {
                "user_id": a.user_id,
                "alert_type": a.alert_type,
                "level": a.level.value,
                "message": a.message,
                "location": {"lat": a.location[0], "lon": a.location[1]},
                "timestamp": a.timestamp.isoformat(),
                "acknowledged": a.acknowledged,
                "caregiver_notified": a.caregiver_notified
            }
            for a in user_alerts[:limit]
        ]
    }