"""
Frequent Places & Location Check Tool for Finding Dory 

This module manages a user's frequently visited places and provides
location-awareness features for dementia assistance. It connects with
Google Maps Geocoding API (with fallback stubs) and persists user
location data in AWS S3.

Core features:
- Add frequent places by geocoding an address
- Retrieve a user’s frequent places
- Check if a query location matches known places
- Provide suggestions and safety alerts if user is at an unknown place

S3 Structure:
- User JSON stored at: s3://findingdoryuserdata/users/{user_id}.json

JSON Structure (partial):
{
  "user_id": 1,
  "frequent_places": [
      {"label": "NTU North Spine", "lat": 1.348, "lon": 103.683, "added_at": 1694001234}
  ],
  "memory_triggers": [...],
  ...
}
"""

import re
import json
import time
import random
import requests
import boto3
from botocore.exceptions import ClientError

# ---------- CONFIG ----------
REGION = "us-east-1"
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"  # replace if needed
BUCKET = "findingdoryuserdata"
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


# ---------- S3 helpers ----------
def get_user_json(user_id: int):
    """
    Retrieve a user's profile JSON from S3.
    If not found, return a skeleton structure with empty lists.

    Args:
        user_id (int): User ID.

    Returns:
        dict: User data including frequent_places and memory_triggers.
    """
    key = f"users/{user_id}.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return {"user_id": user_id, "frequent_places": [], "memory_triggers": []}
        raise


def put_user_json(user_id: int, data: dict):
    """
    Save or update a user's JSON profile back to S3.

    Args:
        user_id (int): User ID.
        data (dict): User JSON object.

    Side effects:
        Overwrites the JSON file in the S3 bucket.
    """
    key = f"users/{user_id}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json"
    )
    print(f"✅ S3 updated: {key}")


# ---------- Geocoding helpers ----------
def geocode_google_maps(address: str):
    """
    Geocode an address using Google Maps API.

    Args:
        address (str): Human-readable address (e.g., "Changi Airport, Singapore").

    Returns:
        tuple[float, float] or None: (latitude, longitude) if found, else None.
    """
    import os
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("❌ GOOGLE_MAPS_API_KEY not set")
        return None

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": api_key}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            print("Geocode error:", data.get("status"), data.get("error_message"))
            return None
        result = data["results"][0]
        loc = result["geometry"]["location"]
        return loc["lat"], loc["lng"]
    except Exception as e:
        print("Google geocode error:", e)
        return None


def geocode_address(address: str):
    """
    Unified geocoding function.
    - Tries Google Maps first.
    - Can later extend to OneMap or other fallback.

    Args:
        address (str): Address string.

    Returns:
        tuple[float, float] or None: Coordinates if available.
    """
    coords = geocode_google_maps(address)
    if coords:
        return coords
    # fallback to OneMap (placeholder, currently re-using Google)
    return geocode_google_maps(address)  # type: ignore


# ---------- Frequent Places tools ----------
def add_frequent_place_tool(user_id: int, name: str, address: str,
                            category: str, visit_frequency: str, notes: str = ""):
    """
    Add a new frequent place for the user with richer metadata.

    Args:
        user_id (int): User ID.
        name (str): User-given name/label for the place.
        address (str): Full postal address.
        category (str): Type of visit (e.g., shopping, medical, leisure).
        visit_frequency (str): How often the user visits (e.g., daily, weekly).
        notes (str, optional): Extra notes (e.g., "Go with daughter").

    Returns:
        dict: {"ok": True, "place": {...}} if added successfully,
              {"error": "..."} if geocoding fails.
    """
    coords = geocode_address(address)
    if not coords:
        return {"error": f"Could not geocode address: {address}"}
    lat, lon = coords
    data = get_user_json(user_id)

    place = {
        "name": name,
        "address": address,
        "lat": lat,
        "lon": lon,
        "category": category,
        "visit_frequency": visit_frequency,
        "notes": notes,
        "added_at": int(time.time())
    }

    data.setdefault("frequent_places", []).append(place)
    put_user_json(user_id, data)
    return {"ok": True, "place": place}


def get_frequent_places_tool(user_id: int):
    """
    Retrieve the list of frequent places for a user.

    Args:
        user_id (int): User ID.

    Returns:
        dict: {"ok": True, "frequent_places": [...]} with all saved places.
    """
    data = get_user_json(user_id)
    return {"ok": True, "frequent_places": data.get("frequent_places", [])}


def check_location(user_id: int, query_address: str):
    """
    Check if the query address matches any known frequent place.

    Args:
        user_id (int): User ID.
        query_address (str): Address the user is currently at.

    Returns:
        dict:
            If match: {"ok": True, "message": "You are at your known location: X ✅"}
            If not:   {"ok": False, "message": "...suggestions..."}
    """
    data = get_user_json(user_id)
    freq_places = data.get("frequent_places", [])
    # normalize addresses for simple matching
    freq_labels = [p["name"].lower() for p in freq_places]
    if query_address.lower() in freq_labels:
        return {"ok": True, "message": f"You are at your known location: {query_address} ✅"}
    else:
        suggestions = "\n".join([f"- {p['name']}" for p in freq_places])
        return {
            "ok": False,
            "message": (
                f"{query_address} is not in your frequently visited places.\n"
                f"Here are your known places:\n{suggestions}\n"
                f"If you plan to travel alone here, I can notify your family."
            )
        }