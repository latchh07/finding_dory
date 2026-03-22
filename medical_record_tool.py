"""
medical_record_tool.py

Handles user medical information including:
- Conditions, allergies, notes
- Medications with frequency, status, and notification state
- Appointments with start/end strings, day, status, and notification state

All timestamps are user-friendly strings for input. Notification tools handle conversions.
"""

import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

import boto3
from botocore.exceptions import ClientError

__all__ = [
    "save_medical_info",
    "get_medical_info",
    "log_medication_intake",
    "add_doctor_appointment",
    "list_upcoming_appointments",
    # NEW:
    "upsert_medication",
    "update_medication_status",
    "delete_medication",
    "list_medication_reminders",
]

# ---------- CONFIG ----------
REGION = "us-east-1"
BUCKET = "findingdoryuserdata"

s3 = boto3.client("s3", region_name=REGION)

# ---------- JSON Helpers ----------
def get_user_json(user_id: int) -> dict:
    """
    Retrieve user JSON data from S3. Returns default structure if not found.
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
                "last_location": None,
                "safety_alerts": [],
                "daily_checklist": {"completed": False, "date": None},
                "medical": {
                    "conditions": [],
                    "allergies": [],
                    "medications": [],   # [{"name","dosage","frequency","notes","status","notification_state"}]
                    "notes": "",
                    "daily_log": [],     # [{medication,dose,taken_at,notes}]
                    "appointments": []   # [{id,title,doctor,start,end,day,location,notes,status,notification_state,created_at}]
                }
            }
        raise

def put_user_json(user_id: int, data: dict) -> None:
    """
    Save user JSON data to S3.
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

def _ensure_medical(data: dict) -> dict:
    """
    Ensure medical structure exists in the data dict.
    """
    data.setdefault("medical", {})
    m = data["medical"]
    m.setdefault("conditions", [])
    m.setdefault("allergies", [])
    m.setdefault("medications", [])
    m.setdefault("notes", "")
    m.setdefault("daily_log", [])
    m.setdefault("appointments", [])
    return m

# ---------- Core Profile ----------
def save_medical_info(
    user_id: int,
    conditions: Optional[List[str]] = None,
    medications: Optional[List[Dict[str, Any]]] = None,
    allergies: Optional[List[str]] = None,
    notes: Optional[str] = None,
    merge: bool = True,
) -> dict:
    """
    Save or update user's medical info.
    medications format: [{"name":str,"dosage":str,"frequency":str,"notes":str}]
    Adds status='pending' and notification_state=0 to each medication.
    merge=True: update only provided fields.
    merge=False: replace core fields; keep existing daily_log & appointments.
    """
    data = get_user_json(user_id)
    m = _ensure_medical(data)

    if merge:
        if conditions is not None:
            m["conditions"] = list(conditions)
        if medications is not None:
            m["medications"] = _normalize_meds(medications)
        if allergies is not None:
            m["allergies"] = list(allergies)
        if notes is not None:
            m["notes"] = notes
    else:
        preserved_log = m.get("daily_log", [])
        preserved_appts = m.get("appointments", [])
        data["medical"] = {
            "conditions": list(conditions or []),
            "allergies": list(allergies or []),
            "medications": _normalize_meds(medications or []),
            "notes": notes or "",
            "daily_log": preserved_log,
            "appointments": preserved_appts,
        }

    put_user_json(user_id, data)
    return {"ok": True, "medical": data["medical"]}

def get_medical_info(
    user_id: int,
    include_daily_log: bool = False,
    upcoming_only: bool = True,
    days_ahead: int = 365,
) -> dict:
    """
    Retrieve medical info for a user.
    include_daily_log: include logged medication intake
    upcoming_only: filter appointments in the future
    """
    data = get_user_json(user_id)
    m = _ensure_medical(data)

    res = {
        "conditions": m.get("conditions", []),
        "allergies": m.get("allergies", []),
        "medications": m.get("medications", []),
        "notes": m.get("notes", ""),
    }

    # Filter appointments
    appts = []
    for a in m.get("appointments", []):
        if upcoming_only:
            # Compare user-friendly start string to today
            try:
                start_dt = datetime.strptime(a.get("start", ""), "%Y-%m-%d %H:%M")
            except Exception:
                continue
            if start_dt < datetime.utcnow():
                continue
        appts.append(a)
    appts.sort(key=lambda x: x.get("start", ""))
    res["appointments"] = appts

    if include_daily_log:
        res["daily_log"] = m.get("daily_log", [])

    return res

# ---------- Daily Intake ----------
def log_medication_intake(
    user_id: int,
    medication: str,
    dose: Optional[str] = None,
    taken_at: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Log that a user took a medication.
    Updates status of medication to 'finished' and resets notification_state to 0.
    """
    if not medication:
        return {"ok": False, "error": "medication name required"}

    data = get_user_json(user_id)
    m = _ensure_medical(data)
    taken_at = taken_at or datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # Update daily log
    m["daily_log"].append({
        "medication": medication,
        "dose": dose,
        "taken_at": taken_at,
        "notes": notes,
    })
    m["daily_log"] = m["daily_log"][-1000:]

    # Update medication status and notification state
    for med in m.get("medications", []):
        if med.get("name") == medication:
            med["status"] = "finished"
            med["notification_state"] = 0

    put_user_json(user_id, data)
    return {"ok": True, "entry": m["daily_log"][-1]}

# ---------- Appointments ----------
def add_doctor_appointment(
    user_id: int,
    doctor: str,
    start: str,  # user-friendly string: "YYYY-MM-DD HH:MM"
    title: Optional[str] = None,
    end: Optional[str] = None,
    day: Optional[str] = None,  # user-friendly date for notifications
    location: Optional[str] = None,
    notes: Optional[str] = None,
    status: str = "upcoming",
) -> dict:
    """
    Add a doctor appointment. Includes day and notification_state fields.
    notification_state=0: notification not sent yet
    notification_state=1: notification sent, waiting for user response
    """
    if not (doctor and start):
        return {"ok": False, "error": "doctor and start time are required"}

    appt_id = str(uuid.uuid4())
    data = get_user_json(user_id)
    m = _ensure_medical(data)
    day = day or start.split(" ")[0]  # default to date part of start
    appt = {
        "id": appt_id,
        "title": title or "Doctor Appointment",
        "doctor": doctor,
        "start": start,
        "end": end,
        "day": day,
        "location": location,
        "notes": notes,
        "status": status,
        "notification_state": 0,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    }
    m["appointments"].append(appt)
    put_user_json(user_id, data)
    return {"ok": True, "appointment": appt}

def list_upcoming_appointments(user_id: int, days_ahead: int = 365) -> List[dict]:
    """
    Return upcoming appointments for a user, default horizon = 365 days.
    """
    info = get_medical_info(
        user_id=user_id,
        include_daily_log=False,
        upcoming_only=True,
        days_ahead=days_ahead,
    )
    return info.get("appointments", [])

# ---------- Helpers ----------
def _normalize_meds(medications: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalize medication entries: add status='pending' and notification_state=0
    """
    norm = []
    for med in medications:
        name = (med or {}).get("name")
        if not name:
            continue
        norm.append({
            "name": name,
            "dosage": med.get("dosage"),
            "frequency": med.get("frequency"),
            "notes": med.get("notes"),
            "status": "pending",
            "notification_state": 0
        })
    return norm

# ---------- Medication helpers (name-based) ----------
def _find_med_index_by_name(meds: List[Dict[str, Any]], name: str) -> int:
    """Return index of med whose name matches (case-insensitive), else -1."""
    target = (name or "").strip().lower()
    for i, m in enumerate(meds or []):
        if (m.get("name") or "").strip().lower() == target:
            return i
    return -1

def _ensure_med_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize/ensure fields on a single medication payload.
    """
    return {
        "name": payload.get("name"),
        "dosage": payload.get("dosage"),
        "frequency": payload.get("frequency"),
        "notes": payload.get("notes"),
        "status": payload.get("status", "pending"),
        "notification_state": int(payload.get("notification_state", 0) or 0),
    }

# ---------- CRUD: upsert / update-status / delete / list ----------
def upsert_medication(
    user_id: int,
    name: str,
    dosage: Optional[str] = None,
    frequency: Optional[str] = None,
    notes: Optional[str] = None,
    status: str = "pending",
    notification_state: Optional[int] = None,
) -> dict:
    """
    Create or update a medication (matched by name, case-insensitive).
    - Defaults: status='pending', notification_state=0
    - Setting status back to 'pending' will re-arm notifications (notification_state -> 0 if not provided).
    """
    if not name:
        return {"ok": False, "error": "name is required"}

    data = get_user_json(user_id)
    m = _ensure_medical(data)
    meds = m.get("medications", [])

    idx = _find_med_index_by_name(meds, name)
    if idx >= 0:
        # Update existing
        existing = meds[idx]
        updated = {
            "name": existing.get("name") or name,
            "dosage": dosage if dosage is not None else existing.get("dosage"),
            "frequency": frequency if frequency is not None else existing.get("frequency"),
            "notes": notes if notes is not None else existing.get("notes"),
            "status": status if status is not None else existing.get("status", "pending"),
            "notification_state": (
                int(notification_state)
                if notification_state is not None
                else existing.get("notification_state", 0)
            ),
        }
        # If we (re)set to pending and caller didn't specify notification_state, re-arm (0)
        if updated["status"] == "pending" and notification_state is None:
            updated["notification_state"] = 0

        meds[idx] = _ensure_med_fields(updated)
        put_user_json(user_id, data)
        return {"ok": True, "upserted": True, "medication": meds[idx]}
    else:
        # Insert new
        new_med = _ensure_med_fields({
            "name": name,
            "dosage": dosage,
            "frequency": frequency,
            "notes": notes,
            "status": status or "pending",
            "notification_state": 0 if notification_state is None else int(notification_state),
        })
        meds.append(new_med)
        m["medications"] = meds
        put_user_json(user_id, data)
        return {"ok": True, "created": True, "medication": new_med}

def update_medication_status(
    user_id: int,
    name: str,
    status: str,
    notification_state: Optional[int] = None,
) -> dict:
    """
    Update a medication's status by name. Optionally set notification_state.
    Common statuses: 'pending', 'paused', 'finished'
    - If status='pending' and notification_state not provided, we re-arm notifications (0).
    - If status='finished' and notification_state not provided, we clear notifications (0).
    """
    if not (name and status):
        return {"ok": False, "error": "name and status are required"}

    data = get_user_json(user_id)
    m = _ensure_medical(data)
    meds = m.get("medications", [])

    idx = _find_med_index_by_name(meds, name)
    if idx < 0:
        return {"ok": False, "error": f"medication '{name}' not found"}

    med = meds[idx]
    med["status"] = status

    if notification_state is not None:
        med["notification_state"] = int(notification_state)
    else:
        # Sensible defaults based on status
        if status == "pending":
            med["notification_state"] = 0
        elif status == "finished":
            med["notification_state"] = 0

    meds[idx] = _ensure_med_fields(med)
    put_user_json(user_id, data)
    return {"ok": True, "medication": meds[idx]}

def delete_medication(user_id: int, name: str) -> dict:
    """
    Delete a medication by name (case-insensitive). Returns count removed.
    """
    if not name:
        return {"ok": False, "error": "name is required"}

    data = get_user_json(user_id)
    m = _ensure_medical(data)
    meds = m.get("medications", [])

    before = len(meds)
    target = (name or "").strip().lower()
    meds = [med for med in meds if (med.get("name") or "").strip().lower() != target]
    removed = before - len(meds)

    m["medications"] = meds
    put_user_json(user_id, data)
    return {"ok": True, "removed": removed}

def list_medication_reminders(user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return medications, optionally filtered by status (e.g., 'pending').
    """
    data = get_user_json(user_id)
    m = _ensure_medical(data)
    meds = m.get("medications", [])
    if status:
        meds = [med for med in meds if (med.get("status") or "").lower() == status.lower()]
    return meds
