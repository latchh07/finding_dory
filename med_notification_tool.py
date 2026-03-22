"""
med_notification_tool.py

Finding Dory — Streamlit Notification Backend (replaces Adalo)

Summary:
- Checks scheduled medications every minute and appointments daily at 9:00.
- Generates human-friendly notification text via Claude (Bedrock).
- Stores generated notifications in NOTIFICATION_LOG per user.
- Streamlit polls /notifications/{user_id} to display notifications.
- Streamlit calls /medications/{user_id}/taken to acknowledge (stop) notifications.
- Scheduler runs in background via APScheduler started on FastAPI startup (no blocking while loop).

Config:
- Set AWS credentials accessible to boto3.
- Ensure Bedrock access available and MODEL_ID correct.
- Run backend:
    uvicorn notifications_streamlit:app --reload --host 0.0.0.0 --port 8000
- Run Streamlit UI:
    streamlit run notifications_streamlit.py --server.port 8501
  (Streamlit will call the backend at http://localhost:8000)
"""

import json
import os
import random
from datetime import datetime, timedelta
from typing import Dict, Any, List
import re
import boto3
import requests
from fastapi import FastAPI, APIRouter, Query, HTTPException
from apscheduler.schedulers.background import BackgroundScheduler
from botocore.exceptions import ClientError

# -------------------------
# Configuration
# -------------------------
REGION = "us-east-1"
S3_BUCKET = "findingdoryuserdata"

# Bedrock / Claude model id (keep as you had it)
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"

# When True the "send" is a log entry to be consumed by Streamlit UI
USE_STREAMLIT_NOTIFICATIONS = True

# Default demo user 
DEFAULT_USER_ID = 1
MEMORY_API_URL = "http://localhost:8000/api/memory/start"

# -------------------------
# AWS clients
# -------------------------
s3 = boto3.client("s3", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# -------------------------
# Scheduler
# -------------------------
scheduler = BackgroundScheduler()

# -------------------------
# In-memory notification store (Streamlit polls this)
# Structure: { user_id: [ {id, time_iso, title, body, metadata}, ... ] }
NOTIFICATION_LOG: Dict[int, List[Dict[str, Any]]] = {}

# -------------------------
# S3 Utilities
# -------------------------
def get_user_json(user_id: int) -> dict:
    key = f"users/{user_id}.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound", ""):
            # default structure when user file doesn't exist
            return {"user_id": user_id, "medical": {"medications": [], "appointments": []}, "memory_triggers": []}
        raise

def put_user_json(user_id: int, data: dict) -> None:
    key = f"users/{user_id}.json"
    data["last_updated"] = datetime.utcnow().isoformat() + "Z"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

def generate_presigned_url(s3_key: str, expiry: int = 3 * 3600) -> str:
    if not s3_key:
        return None
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": s3_key}, ExpiresIn=expiry
    )

# -------------------------
# Claude / Bedrock helpers
# -------------------------
def generate_notification_text(context: str) -> str:
    """
    Generate a short, friendly notification text using Claude on Bedrock.
    Returns the assistant text as a string.
    """
    try:
        resp = bedrock.converse(
            modelId=MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": f"Please produce a short, friendly push-notification style sentence (one or two lines) for the following context: {context}\n\nBe warm and concise."
                        }
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 100, "temperature": 0.7},
        )
        output_message = resp["output"]["message"]["content"][0]["text"]
        return output_message.strip()
    except Exception as e:
        # fallback text if Bedrock fails
        return f"⏰ Reminder: {context}"

def claude_compare_meaning(user_input: str, reference_desc: str) -> Dict[str, Any]:
    """
    Uses Claude to decide whether the user's input matches the reference description.
    Returns a dict: {match: bool, feedback: friendly_text}
    """
    prompt = (
        f"You are a warm, helpful assistant. The memory description is:\n\n\"{reference_desc}\"\n\n"
        f"The user answered: \"{user_input}\"\n\n"
        "Question 1: Does the user's answer capture the same meaning as the memory description? "
        "Answer with a single word: Yes or No.\n\n"
        "Question 2: Provide a short (one-sentence) warm, friendly feedback message suitable to show the user.\n\n"
        "Return both pieces of information in plain text (first line Yes/No, second line the message)."
    )
    try:
        resp = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 150, "temperature": 0.7},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
        # Expect first line contains Yes or No
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        match = False
        feedback = ""
        if lines:
            first = lines[0].lower()
            match = first.startswith("yes")
            feedback = " ".join(lines[1:]) if len(lines) > 1 else ""
        return {"match": match, "feedback": feedback or text}
    except Exception as e:
        return {"match": False, "feedback": "Thanks — got your answer. We'll try again next time!"}

# -------------------------
# Notification helpers (Streamlit-friendly)
# -------------------------
def add_notification_for_user(user_id: int, title: str, body: str, metadata: dict = None) -> None:
    """
    Store a notification for a user. Streamlit will poll /notifications/{user_id}.
    """
    user_id = int(user_id)
    entry = {
        "id": f"{user_id}-{int(datetime.utcnow().timestamp()*1000)}-{random.randint(0,9999)}",
        "time": datetime.utcnow().isoformat() + "Z",
        "title": title,
        "body": body,
        "metadata": metadata or {}
    }
    NOTIFICATION_LOG.setdefault(user_id, []).insert(0, entry)  # newest first

def clear_notifications_for_user(user_id: int) -> None:
    NOTIFICATION_LOG[int(user_id)] = []

# -------------------------
# Notification logic (streamlit-style)
# -------------------------
def notify_medication_streamlit(user_id: int, med: dict):
    """
    Generate a Claude message for medication and store it for Streamlit to display.
    This replaces Adalo push behavior.
    """
    # Parse med time — keep your parse_medication_time function or use the one below if present
    # We'll reparse using a simple approach: med['frequency'] expected like "Everyday at 12pm" or "12:00 pm"
    med_time = parse_medication_time(med.get("frequency", ""))  # helper below
    now = datetime.now().replace(second=0, microsecond=0)
    if not med_time or now < med_time:
        return

    # initialize fields if missing
    med.setdefault("notification_state", 0)
    med.setdefault("status", "pending")

    if med["status"] == "pending" and med["notification_state"] in (0, 1):
        context = f"Remind the user to take {med.get('name')} ({med.get('dosage','')})."
        body = generate_notification_text(context)
        # attach med name so Streamlit can call taken endpoint
        metadata = {"type": "medication", "med_name": med.get("name")}
        add_notification_for_user(user_id, "Medication Reminder", body, metadata)
        med["notification_state"] = 1

        # persist med notification_state back to S3
        data = get_user_json(user_id)
        for m in data.get("medical", {}).get("medications", []):
            if m.get("name") == med.get("name"):
                m.update(med)
        put_user_json(user_id, data)


def mark_medication_taken(user_id: int, med_name: str):
    """
    Called by Streamlit when user confirms 'Taken'.
    Set med.status to finished and notification_state to 0.
    Also remove outstanding notifications for that med in NOTIFICATION_LOG.
    Triggers memory reminders if successful.
    """
    data = get_user_json(user_id)
    changed = False
    for m in data.get("medical", {}).get("medications", []):
        if m.get("name") == med_name:
            m["status"] = "finished"
            m["notification_state"] = 0
            changed = True
    if changed:
        put_user_json(user_id, data)
        # Remove outstanding notifications of this med from log
        uid = int(user_id)
        NOTIFICATION_LOG.setdefault(uid, [])
        NOTIFICATION_LOG[uid] = [
            n for n in NOTIFICATION_LOG.get(uid, [])
            if not (
                n.get("metadata", {}).get("type") == "medication"
                and n.get("metadata", {}).get("med_name") == med_name
            )
        ]

        # 🚀 Trigger memory reminder flow
        try:
            resp = requests.post(MEMORY_API_URL, params={"user_id": user_id})
            if resp.status_code == 200:
                print(f"Memory reminder flow started for user {user_id}")
            else:
                print(f"⚠️ Failed to start memory reminders: {resp.text}")
        except Exception as e:
            print(f"❌ Error calling memory reminders: {e}")

    return changed

def notify_appointment_streamlit(user_id: int, appt: dict):
    """Create appointment reminder (1 day before) for Streamlit UI."""
    appt_day = parse_day_string(appt.get("day", ""))
    if not appt_day:
        return

    now_date = datetime.now().date()
    if (appt_day.date() - now_date) != timedelta(days=1):
        return

    appt.setdefault("notification_state", 0)
    if appt["status"] == "upcoming" and appt["notification_state"] in (0,1):
        context = f"Appointment tomorrow: {appt.get('title','No title')} with {appt.get('doctor','No doctor')}."
        body = generate_notification_text(context)
        metadata = {"type": "appointment", "appt_id": appt.get("id")}
        add_notification_for_user(user_id, "Appointment Reminder", body, metadata)
        appt["notification_state"] = 1

        data = get_user_json(user_id)
        for a in data.get("medical", {}).get("appointments", []):
            if a.get("id") == appt.get("id"):
                a.update(appt)
        put_user_json(user_id, data)

# -------------------------
# Parsing
# -------------------------
def parse_medication_time(frequency_str):
    """
    Converts various time formats to datetime object today.
    Handles:
    - 12pm, 5pm, 5:30pm
    - 5.56pm (decimal format)
    - 12:30 am
    Returns None if parsing fails.
    """
    # Extract only the time part, handle both : and . separators
    match = re.search(r'(\d{1,2})(?:[:.]\s*(\d{2}))?\s*(am|pm)', frequency_str, re.IGNORECASE)
    if not match:
        print(f"⚠️ Could not parse time from: {frequency_str}")
        return None

    hour = int(match.group(1))
    # Convert decimal minutes to integer
    minute = int(float(match.group(2)) if match.group(2) else 0)
    period = match.group(3).lower()

    # Handle AM/PM conversion
    if period == "am":
        if hour == 12:  # midnight
            hour = 0
    elif period == "pm":
        if hour != 12:  # afternoon/evening
            hour += 12

    try:
        return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError as e:
        print(f"❌ Invalid time for '{frequency_str}': hour={hour}, minute={minute}")
        return None

def parse_day_string(day_str: str):
    """
    Converts user-friendly day string (e.g., 'Sept 08') to datetime object.
    """
    try:
        dt = datetime.strptime(day_str, "%b %d")
        dt = dt.replace(year=datetime.now().year)
        return dt
    except:
        return None

# -------------------------
# Scheduler jobs (background)
# -------------------------
def check_medications_job():
    """
    Run every minute: check all users' meds and create notifications (Streamlit) where needed.
    """
    # NOTE: scanning all users in S3 may be expensive at scale.
    # For demo / hackathon we assume small user set; here we check default USER only.
    user_id = DEFAULT_USER_ID
    data = get_user_json(user_id)
    for med in data.get("medical", {}).get("medications", []):
        notify_medication_streamlit(user_id, med)

def check_appointments_job():
    """
    Run daily at 09:00: check appointments for notifications.
    """
    user_id = DEFAULT_USER_ID
    data = get_user_json(user_id)
    for appt in data.get("medical", {}).get("appointments", []):
        notify_appointment_streamlit(user_id, appt)

# Add APScheduler jobs
scheduler.add_job(check_medications_job, "interval", minutes=1, id="check_meds")
scheduler.add_job(check_appointments_job, "cron", hour=9, minute=0, id="check_appts")

# -------------------------
# FastAPI app & endpoints
# -------------------------
app = FastAPI(title="Finding Dory Notifications Backend (Streamlit)")

router = APIRouter(prefix="/api", tags=["notifications"])

@router.get("/notifications/{user_id}")
def get_notifications(user_id: int):
    """
    Return current notifications for this user (newest first).
    Streamlit should poll this endpoint every few seconds or call when user opens UI.
    """
    return NOTIFICATION_LOG.get(int(user_id), [])

@router.post("/medications/{user_id}/taken")
def post_med_taken(user_id: int, med_name: str = Query(...)):
    """
    Endpoint Streamlit calls when user clicks "Taken".
    Returns {"ok": True} if succeeded.
    """
    success = mark_medication_taken(user_id, med_name)
    if not success:
        raise HTTPException(status_code=404, detail="Medication not found")
    return {"ok": True}

@router.post("/notifications/{user_id}/clear")
def clear_user_notifications(user_id: int):
    NOTIFICATION_LOG[int(user_id)] = []
    return {"ok": True}

app.include_router(router)

# Start scheduler on startup (safe, does not block)
@app.on_event("startup")
def _start_scheduler():
    if not scheduler.running:
        scheduler.start()

# -------------------------
# Streamlit UI (function)
# -------------------------
def streamlit_app(frontend_backend_url: str = "http://localhost:8000", user_id: int = DEFAULT_USER_ID):
    """
    Streamlit app function. Either run this script with `streamlit run notifications_streamlit.py`
    or import and call streamlit_app() inside another Streamlit file.

    Behavior:
    - Polls backend /api/notifications/{user_id} every 5 seconds (configurable)
    - Displays each notification as a card with a "Taken" button for medication notifications
    - When "Taken" pressed, calls backend /medications/{user_id}/taken?med_name=...
    """
    import streamlit as st
    import time
    st.set_page_config(page_title="Finding Dory — Notifications", layout="centered")

    st.title("Finding Dory — Notifications")
    st.markdown("This view displays notifications generated by the backend (Claude).")

    poll_interval = st.number_input("Poll interval (seconds)", min_value=2, max_value=60, value=5)
    st.write("Click **Refresh** or wait to poll the backend for new notifications.")

    refresh = st.button("Refresh now")
    if refresh:
        # small client-side fetch (Streamlit can call backend directly)
        pass

    # polling loop (simple)
    last_seen = st.session_state.get("last_notif_time", None)

    col1, col2 = st.columns([3,1])
    with col1:
        st.write("Notifications (newest first):")

    # polling and display
    if "notifications_cache" not in st.session_state:
        st.session_state["notifications_cache"] = []

    # Poll backend once per user interaction or every poll_interval seconds
    if "last_poll" not in st.session_state:
        st.session_state["last_poll"] = 0

    if refresh or (time.time() - st.session_state["last_poll"] > poll_interval):
        try:
            resp = requests.get(f"{frontend_backend_url}/api/notifications/{user_id}", timeout=5)
            resp.raise_for_status()
            st.session_state["notifications_cache"] = resp.json()
            st.session_state["last_poll"] = time.time()
        except Exception as e:
            st.error(f"Could not fetch notifications: {e}")

    # Display cached notifications
    notifs = st.session_state.get("notifications_cache", [])
    if not notifs:
        st.info("No notifications yet.")
    else:
        for n in notifs:
            with st.container():
                st.markdown(f"**{n['title']}** — *{n['time']}*")
                st.write(n['body'])
                # metadata handling
                meta = n.get("metadata", {})
                if meta.get("type") == "medication":
                    med_name = meta.get("med_name")
                    if st.button(f"Taken — {med_name}", key=f"take-{n['id']}"):
                        # call backend to mark taken
                        try:
                            r = requests.post(f"{frontend_backend_url}/api/medications/{user_id}/taken", params={"med_name": med_name}, timeout=5)
                            r.raise_for_status()
                            st.success(f"Marked {med_name} as taken.")
                            # remove this notification locally
                            # (backend also removes)
                        except Exception as e:
                            st.error(f"Could not mark taken: {e}")
                elif meta.get("type") == "appointment":
                    if st.button("Acknowledge", key=f"ack-{n['id']}"):
                        # acknowledge appointment by clearing that notification entry
                        # (we keep it simple: clear all notifications for demo)
                        requests.post(f"{frontend_backend_url}/api/notifications/{user_id}/clear")
                        st.success("Acknowledged.")

    # small keep-alive (do not block)
    st.markdown("---")
    st.write("Refresh / Poll to get new notifications.")

# This file defines FastAPI app and streamlit_app function.
# To run backend:
#   uvicorn notifications_streamlit:app --reload --port 8000
#
# To run Streamlit UI (in separate terminal):
#   streamlit run notifications_streamlit.py --server.port 8501
# and then inside Streamlit editor call:
#   from notifications_streamlit import streamlit_app
#   streamlit_app(frontend_backend_url="http://localhost:8000", user_id=1)
#
# Or simply run:
#   streamlit run notifications_streamlit.py
# and Streamlit will load this file; if you want it to auto-run the UI, you can add:
#   if "streamlit" in sys.modules: streamlit_app()






