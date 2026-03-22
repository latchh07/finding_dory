"""
Memory Reminder Tool for Finding Dory - v3
-------------------------------------------
Generates memory notifications with images and Claude-friendly text.

Features:
- Claude generates warm, readable notification messages.
- Mistakes tracked per person; >=3 mistakes biases selection.
- Description hidden in notifications, only used internally for answer checking.
- Streamlit integration: FastAPI endpoints for reminder and answer checking.
"""

import json, requests
import random
from typing import Dict, Any
import asyncio
import boto3
from fastapi import APIRouter, Query, BackgroundTasks
from botocore.exceptions import ClientError

from memories_save_tool import (
    get_user_json,
    generate_presigned_url,
    increment_memory_mistake,
)

# ---------- AWS ----------
REGION = "us-east-1"
S3_BUCKET = "findingdoryuserdata"
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
USER_ID = 1  # default user for testing

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

# ---------- Claude Helpers ----------
def claude_generate_notification(memory_name: str) -> str:
    """
    Use Claude to generate a friendly memory reminder notification.
    """
    prompt = (
        f"Generate a short, warm, friendly push notification asking the user "
        f"to recall their memory about {memory_name}. "
        "Do not reveal the full memory description. Example: 'Do you remember this person?'"
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content":[{"text": prompt}]}],
        inferenceConfig={"maxTokens": 50, "temperature": 0.7}
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


def claude_compare_answer(memory_description: str, user_input: str) -> bool:
    """
    Ask Claude if user's input matches the memory description.
    """
    prompt = (
        f"Memory description: {memory_description}\n"
        f"User input: {user_input}\n\n"
        "Does the user's input capture the same meaning as the description? "
        "Answer only 'Yes' or 'No'."
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content":[{"text": prompt}]}],
        inferenceConfig={"maxTokens": 20, "temperature": 0}
    )
    reply = resp["output"]["message"]["content"][0]["text"].strip().lower()
    return reply.startswith("yes")


def claude_friendly_reply(correct: bool) -> str:
    """
    Ask Claude to generate a warm, encouraging reply for the user.
    """
    status = "correct" if correct else "incorrect"
    prompt = (
        f"The user just answered a memory recall question. Their answer was {status}.\n\n"
        f"Write a short, warm, encouraging message for them. "
        f"Make it readable and friendly, like a supportive assistant. "
        f"Do not return JSON or structured data, just plain text."
    )

    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content":[{"text": prompt}]}],
        inferenceConfig={"maxTokens": 100, "temperature": 0.7}
    )
    return resp["output"]["message"]["content"][0]["text"].strip()

# ---------- Core Logic ----------
def choose_memory_for_reminder(user_id: int) -> Dict[str, Any]:
    """
    Choose a memory for notification. Bias toward people with >=3 mistakes.
    """
    data = get_user_json(user_id)
    mems = data.get("memory_triggers", []) or []
    if not mems:
        return {"found": False, "message": "No memories yet!"}

    # Count mistakes per person
    mistake_counts = {}
    for m in mems:
        name = m.get("name")
        mistake_counts[name] = mistake_counts.get(name, 0) + m.get("mistakes", 0)

    priority_people = [p for p, cnt in mistake_counts.items() if cnt >= 3]
    if priority_people:
        pool = [m for m in mems if m["name"] in priority_people]
        chosen = random.choice(pool) if pool else random.choice(mems)
    else:
        chosen = random.choice(mems)

    # Presign image
    key = chosen.get("image")
    url = generate_presigned_url(key) if key else None

    # Claude generates the actual notification message
    notif_message = claude_generate_notification(chosen.get("name"))

    return {
        "found": True,
        "title": "🧠 Memory Reminder",
        "message": notif_message,
        "image_url": url,
        "memory_name": chosen.get("name"),
        "s3_key": key,
        "full_description": chosen.get("description"),  # for answer checking only
    }
    

active_reminders = {}  # track if a user has active reminder loop

async def run_memory_reminders(user_id: int):
    """
    Run memory reminders every 20s until:
    - all memories are shown once, OR
    - user sends 'quit' via /api/memory/stop
    """
    if active_reminders.get(user_id):
        return  # already running

    active_reminders[user_id] = True
    print(f"🚀 Starting memory reminders for user {user_id}")

    shown = set()
    while active_reminders.get(user_id, False):
        payload = choose_memory_for_reminder(user_id)

        # if no more memories left, stop
        if not payload.get("found") or payload["s3_key"] in shown:
            print("✅ All memories done, stopping reminders.")
            break

        shown.add(payload["s3_key"])

        # Instead of printing, notify Streamlit via DB/Redis/WebSocket
        # For now, just log it
        print(f"🔔 Reminder for {payload['memory_name']} -> {payload['image_url']}")

        # wait 20s
        await asyncio.sleep(20)

    active_reminders[user_id] = False
    print(f"🛑 Memory reminder loop ended for user {user_id}")
    

# ---------- FastAPI ----------
router = APIRouter(prefix="/api/memory", tags=["memory"])

@router.post("/start")
async def api_start_reminders(user_id: int = Query(1), background_tasks: BackgroundTasks = None):
    """
    Starts automated memory reminders every 20s for a user.
    Runs until all memories shown OR user stops.
    """
    background_tasks.add_task(run_memory_reminders, user_id)
    return {"ok": True, "message": f"Started memory reminders for user {user_id}"}


@router.post("/stop")
def api_stop_reminders(user_id: int = Query(1)):
    """
    Stops the automated memory reminders early (like if user types quit).
    """
    active_reminders[user_id] = False
    return {"ok": True, "message": f"Stopped memory reminders for user {user_id}"}

@router.get("/reminder")
def api_reminder(user_id: int = Query(1)):
    """
    Returns one memory payload for Streamlit.
    Includes presigned image URL and Claude-generated message.
    """
    payload = choose_memory_for_reminder(user_id)
    # Strip out description before sending to frontend
    payload.pop("full_description", None)
    return payload


@router.get("/check_answer")
def api_check_answer(
    user_id: int = Query(USER_ID),
    memory_name: str = Query(...),
    user_input: str = Query(...),
):
    """
    Check if user's answer matches the memory description.
    Increments mistakes if wrong.
    """
    data = get_user_json(user_id)
    mems = data.get("memory_triggers", [])
    memory = next((m for m in mems if m["name"] == memory_name), None)

    if not memory:
        return {"ok": False, "message": "Memory not found."}

    correct = claude_compare_answer(memory.get("description", ""), user_input)

    if not correct:
        increment_memory_mistake(user_id, memory_name, memory.get("description", ""))
        feedback = claude_friendly_reply(False)
        return {"ok": True, "correct": False, "message": feedback}

    else:
        feedback = claude_friendly_reply(True)
        return {"ok": True, "correct": True, "message": feedback}
    
    

# requests.post("http://localhost:8000/api/memory/start", params={"user_id":1})