"""
contact_tools.py
================
S3-backed contacts manager for Finding Dory.

What it does
------------
- Stores contacts inside each user's JSON at:  s3://findingdoryuserdata/users/{user_id}.json
- Lets you add/upsert/list contacts and mark a **primary emergency** contact.
- Can write the **last known location** into the same JSON.
- Builds **deep-link payloads** for emergency actions:
    - tel:      opens the dialer (call)
    - sms:      opens the SMS composer with a prefilled message
    - WhatsApp: opens a chat via wa.me with a prefilled message
  The message can include a Google Maps link to the current/last location.

JSON shape (excerpt)
--------------------
{
  "user_id": 1,
  "last_location": {"lat": 1.3521, "lng": 103.8198, "timestamp": "2025-09-06T14:25:00Z"},
  "contacts": [
    {
      "id": "uuid",
      "name": "Sarah Lim",
      "phone": "+6598765432",
      "relation": "daughter",
      "type": "emergency",
      "primary": true,
      "notify_via": ["call","sms","whatsapp"],
      "notes": "Prefer SMS during work hours",
      "created_at": "2025-09-06T14:22:10Z"
    }
  ]
}

Deep-link docs (reference)
--------------------------
- tel:      (Phone URL scheme)  iOS: https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/PhoneLinks/PhoneLinks.html
- sms:      (SMS URL scheme)    iOS: https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/SMSLinks/SMSLinks.html
            Android intents:    https://developer.android.com/guide/components/intents-common#ComposeMessage
- WhatsApp: Click-to-Chat       https://wa.me/
  (WhatsApp FAQ pages change URLs occasionally; wa.me is the canonical entry point.)

If you expose these helpers through FastAPI (see shim below),
your interactive API docs will be at:  http://127.0.0.1:8010/docs
"""

from __future__ import annotations

import json
import uuid
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from urllib.parse import quote_plus

import boto3
from botocore.exceptions import ClientError

# ---------- CONFIG ----------
REGION = "us-east-1"
BUCKET = "findingdoryuserdata"

# Allowed enumerations (light validation)
CONTACT_TYPES = {"emergency", "caregiver", "doctor", "family", "other"}
NOTIFY_CHANNELS = {"call", "sms", "whatsapp"}

s3 = boto3.client("s3", region_name=REGION)


# ---------- S3 JSON helpers ----------
def get_user_json(user_id: int) -> dict:
    """
    Load users/{user_id}.json from S3.
    If it doesn't exist, return a minimal scaffold compatible with the rest of the app.
    """
    key = f"users/{user_id}.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return {
                "user_id": user_id,
                "frequent_places": [],
                "memory_triggers": [],
                "last_location": None,  # {"lat": float, "lng": float, "timestamp": str}
                "safety_alerts": [],
                "daily_checklist": {"completed": False, "date": None},
                "medical": {
                    "conditions": [],
                    "allergies": [],
                    "medications": [],
                    "notes": "",
                    "daily_log": [],
                    "appointments": []
                },
                "contacts": []  # filled by this module
            }
        raise


def put_user_json(user_id: int, data: dict) -> None:
    """
    Persist the user's JSON back to S3 with a fresh last_updated timestamp.
    """
    key = f"users/{user_id}.json"
    data["last_updated"] = datetime.utcnow().isoformat() + "Z"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"✅ S3 updated: {key}")


def _ensure_contacts(data: dict) -> List[dict]:
    """
    Ensure the 'contacts' array exists on the user JSON and return it.
    """
    data.setdefault("contacts", [])
    return data["contacts"]


# ---------- Phone helpers ----------
def _normalize_phone(phone: str) -> str:
    """
    Normalize phone numbers for consistent matching.
    - Keeps only digits and a leading '+' if present.
    - Does NOT auto-add a country code (your client should capture +65 for SG).
    """
    phone = (phone or "").strip()
    if phone.startswith("+"):
        return "+" + re.sub(r"\D", "", phone[1:])
    return re.sub(r"\D", "", phone)


# ---------- Contact CRUD ----------
def add_contact(
    user_id: int,
    name: str,
    phone: str,
    relation: Optional[str] = None,
    type: str = "emergency",
    primary: bool = False,
    notify_via: Optional[List[str]] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Add or **upsert** a single contact (matched by normalized phone).
    - If the phone already exists, we update fields (name/relation/type/notify_via/notes).
    - If `primary=True`, this contact becomes the only primary; all others are set to False.
    - Ensures at least one primary exists once you have any contacts.

    Returns: {"ok": True, "contact": {...}, "upserted": bool}
    """
    if not (name and phone):
        return {"ok": False, "error": "name and phone are required"}

    phone_norm = _normalize_phone(phone)
    kind = (type or "emergency").lower()
    if kind not in CONTACT_TYPES:
        kind = "other"

    channels = notify_via or ["call"]
    channels = [c for c in channels if c in NOTIFY_CHANNELS] or ["call"]

    data = get_user_json(user_id)
    contacts = _ensure_contacts(data)

    # Upsert by phone
    existing = next((c for c in contacts if _normalize_phone(c.get("phone", "")) == phone_norm), None)
    if existing:
        existing.update({
            "name": name,
            "relation": relation,
            "type": kind,
            "notify_via": channels,
            "notes": notes,
        })
        if primary:
            for c in contacts:
                c["primary"] = False 
            existing["primary"] = True
        put_user_json(user_id, data)
        return {"ok": True, "contact": existing, "upserted": True}

    # Create new
    contact = {
        "id": str(uuid.uuid4()),
        "name": name,
        "phone": phone_norm,
        "relation": relation,
        "type": kind,
        "primary": False,
        "notify_via": channels,
        "notes": notes,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    contacts.append(contact)

    # Ensure at least one primary
    if primary or not any(c.get("primary") for c in contacts):
        for c in contacts:
            c["primary"] = False
        contact["primary"] = True

    put_user_json(user_id, data)
    return {"ok": True, "contact": contact, "upserted": False}


def save_contacts(user_id: int, contacts: List[Dict[str, Any]], merge: bool = True) -> dict:
    """
    Bulk save contacts.
    - merge=True: upsert each item by id or normalized phone; preserves others.
    - merge=False: replace the entire contacts list with provided items.
    Ensures there is **exactly one** primary when finished.

    Returns: {"ok": True, "contacts": [...]}
    """
    data = get_user_json(user_id)
    current = _ensure_contacts(data)

    if not merge:
        new_list: List[dict] = []
        for c in contacts:
            if not (c.get("name") and c.get("phone")):
                continue
            kind = (c.get("type") or "emergency").lower()
            if kind not in CONTACT_TYPES:
                kind = "other"
            chans = [ch for ch in (c.get("notify_via") or ["call"]) if ch in NOTIFY_CHANNELS] or ["call"]
            new_list.append({
                "id": c.get("id") or str(uuid.uuid4()),
                "name": c["name"],
                "phone": _normalize_phone(c["phone"]),
                "relation": c.get("relation"),
                "type": kind,
                "primary": bool(c.get("primary", False)),
                "notify_via": chans,
                "notes": c.get("notes"),
                "created_at": c.get("created_at") or (datetime.utcnow().isoformat() + "Z"),
            })
        # Ensure exactly one primary
        primaries = [c for c in new_list if c.get("primary")]
        if len(primaries) == 0 and new_list:
            new_list[0]["primary"] = True
        elif len(primaries) > 1:
            # Keep the most recent as primary
            most_recent = max(primaries, key=lambda c: c.get("created_at", ""))
            for c in new_list:
                c["primary"] = (c is most_recent)

        data["contacts"] = new_list
        put_user_json(user_id, data)
        return {"ok": True, "contacts": new_list}

    # merge=True: build indexes
    by_phone = {_normalize_phone(c.get("phone", "")): c for c in current if c.get("phone")}
    by_id = {c.get("id"): c for c in current if c.get("id")}

    for c in contacts:
        if not c.get("phone") and not c.get("id"):
            continue

        target = by_id.get(c.get("id")) if c.get("id") else by_phone.get(_normalize_phone(c.get("phone", "")))
        kind = (c.get("type") or (target.get("type") if target else "emergency")).lower()
        if kind not in CONTACT_TYPES:
            kind = "other"
        chans = c.get("notify_via", target.get("notify_via") if target else ["call"])
        chans = [ch for ch in (chans or []) if ch in NOTIFY_CHANNELS] or ["call"]

        payload = {
            "name": c.get("name") or (target.get("name") if target else None),
            "phone": _normalize_phone(c.get("phone") or (target.get("phone") if target else "")),
            "relation": c.get("relation", target.get("relation") if target else None),
            "type": kind,
            "primary": bool(c.get("primary", target.get("primary") if target else False)),
            "notify_via": chans,
            "notes": c.get("notes", target.get("notes") if target else None),
        }

        if target:
            target.update(payload)
        else:
            current.append({
                "id": str(uuid.uuid4()),
                **payload,
                "created_at": datetime.utcnow().isoformat() + "Z",
            })

    # Ensure single primary
    primaries = [c for c in current if c.get("primary")]
    if len(primaries) == 0 and current:
        current[0]["primary"] = True
    elif len(primaries) > 1:
        most_recent = max(primaries, key=lambda c: c.get("created_at", ""))
        for c in current:
            c["primary"] = (c is most_recent)

    put_user_json(user_id, data)
    return {"ok": True, "contacts": current}


def list_contacts(
    user_id: int,
    kind: Optional[str] = None,
    relation: Optional[str] = None,
    primary: Optional[bool] = None
) -> List[dict]:
    """
    Return contacts filtered by optional criteria:
    - kind: "emergency" | "caregiver" | "doctor" | "family" | "other"
    - relation: free-text match (e.g., "daughter")
    - primary: True/False (only primaries or only non-primaries)
    """
    data = get_user_json(user_id)
    contacts = _ensure_contacts(data)
    out = contacts
    if kind:
        out = [c for c in out if (c.get("type") or "").lower() == kind.lower()]
    if relation:
        out = [c for c in out if (c.get("relation") or "").lower() == relation.lower()]
    if primary is not None:
        out = [c for c in out if bool(c.get("primary")) == bool(primary)]
    return out


def set_primary_contact(user_id: int, contact_id: Optional[str] = None, phone: Optional[str] = None) -> dict:
    """
    Make the specified contact the **only** primary.
    Target by `contact_id` or by phone number (normalized).
    """
    if not (contact_id or phone):
        return {"ok": False, "error": "provide contact_id or phone"}

    data = get_user_json(user_id)
    contacts = _ensure_contacts(data)

    target = None
    if contact_id:
        target = next((c for c in contacts if c.get("id") == contact_id), None)
    else:
        pn = _normalize_phone(phone or "")
        target = next((c for c in contacts if _normalize_phone(c.get("phone", "")) == pn), None)

    if not target:
        return {"ok": False, "error": "contact not found"}

    for c in contacts:
        c["primary"] = False
    target["primary"] = True
    put_user_json(user_id, data)
    return {"ok": True, "contact": target}


def get_primary_contact(user_id: int) -> Optional[dict]:
    """
    Return the primary contact if present; else the first contact; else None.
    """
    data = get_user_json(user_id)
    contacts = _ensure_contacts(data)
    primary = next((c for c in contacts if c.get("primary")), None)
    return primary or (contacts[0] if contacts else None)


# ---------- Location helper ----------
def update_last_location(user_id: int, lat: float, lng: float, timestamp_iso: Optional[str] = None) -> dict:
    """
    Store the user's most recent location in their JSON.
    This enables emergency payloads to include a Google Maps link.
    """
    data = get_user_json(user_id)
    data["last_location"] = {
        "lat": lat,
        "lng": lng,
        "timestamp": timestamp_iso or (datetime.utcnow().isoformat() + "Z")
    }
    put_user_json(user_id, data)
    return {"ok": True, "last_location": data["last_location"]}


# ---------- Emergency payload ----------
def build_emergency_payload(
    user_id: int,
    current_lat: Optional[float] = None,
    current_lng: Optional[float] = None,
    address: Optional[str] = None,
    contact_id: Optional[str] = None,
    phone_override: Optional[str] = None,
    message_prefix: str = "EMERGENCY: I need help.",
) -> dict:
    """
    Create deep-links and a ready-to-send message for calling or messaging an emergency contact.

    Inputs
    ------
    - current_lat/current_lng: optional runtime location; if not passed, uses 'last_location' from JSON.
    - address: optional human-readable address to include in the message.
    - contact_id / phone_override: target contact; if neither given, uses the primary contact.
    - message_prefix: the prefix for the SMS/WhatsApp text.

    Output
    ------
    {
      "ok": true,
      "contact": {...} | null,
      "phone": "+6598765432",
      "message": "EMERGENCY: ... https://www.google.com/maps?q=LAT,LNG",
      "maps_url": "https://www.google.com/maps?q=LAT,LNG",
      "links": {
        "tel": "tel:+6598765432",
        "sms": "sms:+6598765432?&body=...urlencoded...",
        "whatsapp": "https://wa.me/6598765432?text=...urlencoded..."
      }
    }

    NOTE: This function only **builds** links. Your mobile app or desktop UI should open them.
    """
    data = get_user_json(user_id)

    # Resolve target contact
    contact = None
    if contact_id:
        contact = next((c for c in _ensure_contacts(data) if c.get("id") == contact_id), None)
    if not contact:
        contact = get_primary_contact(user_id)

    if not contact and not phone_override:
        return {"ok": False, "error": "no contact found and no phone_override provided"}

    phone = _normalize_phone(phone_override or (contact.get("phone") if contact else ""))
    if not phone:
        return {"ok": False, "error": "target phone is missing"}

    # Determine location
    lat = current_lat
    lng = current_lng
    if lat is None or lng is None:
        last = data.get("last_location") or {}
        lat = lat if lat is not None else last.get("lat")
        lng = lng if lng is not None else last.get("lng")

    maps_url = f"https://www.google.com/maps?q={lat},{lng}" if (lat is not None and lng is not None) else None

    # Compose message text
    parts = [message_prefix]
    if address:
        parts.append(f"Location: {address}")
    if maps_url:
        parts.append(maps_url)
    msg = " ".join(parts)

    # Deep links
    tel_url = f"tel:{phone}"
    sms_url = f"sms:{phone}?&body={quote_plus(msg)}"
    whatsapp_url = f"https://wa.me/{phone.lstrip('+')}?text={quote_plus(msg)}"

    return {
        "ok": True,
        "contact": contact,
        "phone": phone,
        "message": msg,
        "maps_url": maps_url,
        "links": {"tel": tel_url, "sms": sms_url, "whatsapp": whatsapp_url},
    }
