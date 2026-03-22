# agent_runner.py
from __future__ import annotations
import os, json, re, time
from typing import Dict, Any, List, Optional, Callable, Tuple

import requests
from llm_router import NOVA_LITE_ID, CLAUDE_HAIKU_ID, CLAUDE_SONNET_ID
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

# === Bring in your LLM router ===
from llm_router import generate_text, choose_models

# === Import your existing tool functions ===
# (All of these are from the files you already added)
from medical_record_tool import (
    save_medical_info, get_medical_info, log_medication_intake,
    add_doctor_appointment, list_upcoming_appointments,
    # NEW:
    upsert_medication, update_medication_status, delete_medication, list_medication_reminders,
)
from med_notification_tool import (
    mark_medication_taken,   # keep this from the notifier
)
from contact_tools import (
    add_contact, list_contacts, set_primary_contact, get_primary_contact,
    update_last_location, build_emergency_payload,
)
from emergency_help_tool import find_emergency_help_points
# frequent places tool
from freq_places_tool import (
    add_frequent_place_tool, get_frequent_places_tool, check_location,
)

# Optional: some navigation endpoints live only behind FastAPI routes in app.py.
# We'll call them over HTTP to avoid circular imports.
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000")

# -----------------------------
# Agent config / Guard rails
# -----------------------------
MAX_STEPS = 4
JSON_ONLY_REMINDER = (
    "Return ONLY a single JSON object with keys: type, tool, args OR type, message. "
    "Do not write prose outside JSON. Do not include code fences."
)

# Minimal in-memory session state (per user)
_SESSIONS: Dict[int, List[Dict[str, Any]]] = {}  # {user_id: [{"role": "user|assistant|tool", "content": str|dict}, ...]}

# -----------------------------
# Tool registry (name -> func)
# -----------------------------
# Each tool function must accept **kwargs and return a JSON-serializable dict.
ToolFn = Callable[..., Dict[str, Any]]

def _route_by_name_http(user_id: int, name: str, route_type: str = "walk") -> Dict[str, Any]:
    try:
        r = requests.get(
            f"{AGENT_BASE_URL}/places/route-by-name",
            params={"user_id": user_id, "name": name, "route_type": route_type},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        return {"ok": False, "error": f"route-by-name failed: {r.status_code}", "details": r.text}
    except Exception as e:
        return {"ok": False, "error": f"route-by-name exception: {e}"}

TOOLS: Dict[str, Tuple[str, ToolFn]] = {
    # --- Meds CRUD (medical_record_tool) ---
    "upsert_medication": (
        "Create or update a medication. args: {user_id:int, name:str, dosage?:str, frequency?:str, notes?:str, status?:str, notification_state?:int}",
        lambda **a: upsert_medication(**a),
    ),
    "update_med_status": (
        "Update medication status. args: {user_id:int, name:str, status:str, notification_state?:int}",
        lambda **a: update_medication_status(**a),
    ),
    "delete_medication": (
        "Delete a medication by name. args: {user_id:int, name:str}",
        lambda **a: delete_medication(**a),
    ),
    "list_med_reminders": (
        "List medications (optionally filter by status). args: {user_id:int, status?:str}",
        lambda **a: {"ok": True, "reminders": list_medication_reminders(**a)},
    ),

    # --- Mark taken (notifier) ---
    "mark_med_taken": (
        "Mark medication as taken (finishes & clears notifications). args: {user_id:int, med_name:str}",
        lambda **a: {"ok": mark_medication_taken(
            user_id=a["user_id"],
            med_name=a.get("med_name") or a.get("medication")
        )},
    ),

    # --- Medical info / appointments ---
    "get_medical_info": (
        "Get medical info and upcoming appointments. args: {user_id:int, include_daily_log?:bool, upcoming_only?:bool, days_ahead?:int}",
        lambda **a: get_medical_info(**a),
    ),
    "add_doctor_appointment": (
        "Add a doctor appointment. args: {user_id:int, doctor:str, start:str, title?:str, end?:str, day?:str, location?:str, notes?:str, status?:str}",
        lambda **a: add_doctor_appointment(
            user_id=a["user_id"],
            doctor=a["doctor"],
            start=a.get("start") or a.get("start_iso"),
            title=a.get("title"),
            end=a.get("end") or a.get("end_iso"),
            day=a.get("day"),
            location=a.get("location"),
            notes=a.get("notes"),
            status=a.get("status", "upcoming"),
        ),
    ),
    "list_upcoming_appointments": (
        "List upcoming appointments. args: {user_id:int, days_ahead?:int}",
        lambda **a: {"ok": True, "appointments": list_upcoming_appointments(**a)},
    ),
    # Contacts
    "add_contact": (
        "Add or upsert a contact. args: {user_id:int, name:str, phone:str, relation?:str, type?:str, primary?:bool, notify_via?:[str], notes?:str}",
        lambda **a: add_contact(**a),
    ),
    "list_contacts": (
        "List contacts. args: {user_id:int, kind?:str, relation?:str, primary?:bool}",
        lambda **a: {"ok": True, "contacts": list_contacts(**a)},
    ),
    "set_primary_contact": (
        "Set the primary contact. args: {user_id:int, contact_id?:str, phone?:str}",
        lambda **a: set_primary_contact(**a),
    ),
    "get_primary_contact": (
        "Get the primary contact. args: {user_id:int}",
        lambda **a: ({"ok": True, "contact": get_primary_contact(a["user_id"])}),
    ),
    "build_emergency_payload": (
        "Build tel/sms/whatsapp links for emergency. args: {user_id:int, current_lat?:float, current_lng?:float, address?:str, contact_id?:str, phone_override?:str, message_prefix?:str}",
        lambda **a: build_emergency_payload(**a),
    ),
    "update_last_location": (
        "Update last known location. args: {user_id:int, lat:float, lng:float, timestamp_iso?:str}",
        lambda **a: update_last_location(**a),
    ),

    # Frequent places
    "add_frequent_place": (
        "Add a frequent place by address (geocodes). args: {user_id:int, name:str, address:str, category:str, visit_frequency:str, notes?:str}",
        lambda **a: add_frequent_place_tool(**a),
    ),
    "list_frequent_places": (
        "List frequent places. args: {user_id:int}",
        lambda **a: get_frequent_places_tool(**a),
    ),
    "check_location": (
        "Check if an address matches a known frequent place. args: {user_id:int, query_address:str}",
        lambda **a: check_location(**a),
    ),
    "start_navigation": (
        "Start navigation to a frequent place by name via server route. args: {user_id:int, name:str, route_type?:str}",
        lambda **a: _route_by_name_http(user_id=a["user_id"], name=a["name"], route_type=a.get("route_type","walk")),
    ),

    # Emergency help
    "find_help_points": (
        "Find nearby emergency help points (MRT, polyclinic, hospital, police). args: {lat:float, lon:float, radius_m?:int, per_type_limit?:int}",
        lambda **a: find_emergency_help_points(**a),
    ),
}

# -----------------------------
# Prompt construction
# -----------------------------
def _tool_specs_for_prompt() -> str:
    lines = []
    for name, (desc, _) in TOOLS.items():
        lines.append(f'- {name}: {desc}')
    return "\n".join(lines)

SYSTEM_INSTRUCTIONS = f"""
You are Finding Dory, an assistive agent for people with dementia and caregivers.
You can call tools to schedule medication reminders, manage contacts, find help
points, add frequent places, and start navigation to known places. Always keep
replies brief, supportive, and clear.

When you need to act, output ONLY JSON with this shape:
{{ "type": "tool_call", "tool": "<tool_name>", "args": {{ ... }} }}

When you are done and want to message the user, output ONLY:
{{ "type": "final", "message": "<what to say to the user>" }}

Available tools:
{_tool_specs_for_prompt()}

Rules:
- Prefer the most direct single tool for the user request.
- For scheduling medicines, require due_at_iso with timezone
  (e.g., 2025-09-07T20:30:00+08:00).
- Never reveal chain-of-thought. Keep outputs short.
"""

def _build_user_prompt(user_message: str, last_tool_result: Optional[Dict[str, Any]] = None) -> str:
    parts = [f"User said: {user_message}"]
    if last_tool_result is not None:
        # Summarize tool observation compactly (avoid dumping huge blobs)
        obs = json.dumps(last_tool_result)[:1200]
        parts.append(f"Observation from last tool: {obs}")
    parts.append(JSON_ONLY_REMINDER)
    return "\n\n".join(parts)

# -----------------------------
# JSON parsing helpers
# -----------------------------
def _extract_json(s: str) -> Optional[Dict[str, Any]]:
    s = s.strip()
    # Already JSON?
    try:
        return json.loads(s)
    except Exception:
        pass
    # Try to pull the first {...} block
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

# -----------------------------
# Core loop
# -----------------------------
def agent_act(user_id: int, message: str, max_steps: int = MAX_STEPS) -> Dict[str, Any]:
    history = _SESSIONS.setdefault(user_id, [])
    history.append({"role": "user", "content": message})

    last_obs: Optional[Dict[str, Any]] = None
    actions: List[Dict[str, Any]] = []

    for step in range(max_steps):
        # Ask the LLM what to do next (fast-first planning)
        prefer = [NOVA_LITE_ID, CLAUDE_HAIKU_ID, CLAUDE_SONNET_ID]
        llm_out = generate_text(
        prompt=f"{SYSTEM_INSTRUCTIONS}\n\n{_build_user_prompt(message, last_tool_result=last_obs)}",
        max_tokens=400,
        temperature=0.2,
        prefer=prefer,
        system_prompt=None,
        use_cache=False,
    )
        model_used = llm_out["model_id"]
        parsed = _extract_json(llm_out["text"])
        if not parsed:
            # If the model didn't return JSON, stop gracefully
            final_msg = "Sorry, I couldn't parse that. Could you please rephrase?"
            history.append({"role": "assistant", "content": final_msg})
            return {"ok": True, "model_used": model_used, "steps": actions, "final": final_msg}

        if parsed.get("type") == "final":
            msg = str(parsed.get("message") or "").strip()
            history.append({"role": "assistant", "content": msg})
            return {"ok": True, "model_used": model_used, "steps": actions, "final": msg}

        if parsed.get("type") == "tool_call":
            tool = str(parsed.get("tool") or "").strip()
            args = parsed.get("args") or {}
            if tool not in TOOLS:
                msg = f"Tool '{tool}' is not available."
                history.append({"role": "assistant", "content": msg})
                return {"ok": False, "model_used": model_used, "steps": actions, "final": msg}

            desc, fn = TOOLS[tool]
            try:
                result = fn(**args)
                last_obs = {"tool": tool, "args": args, "result": result}
            except TypeError as e:
                last_obs = {"tool": tool, "args": args, "result": {"ok": False, "error": f"Invalid arguments: {e}"}}
            except Exception as e:
                last_obs = {"tool": tool, "args": args, "result": {"ok": False, "error": f"Tool error: {e}"}}

            actions.append(last_obs)
            history.append({"role": "tool", "content": last_obs})
            # Loop continues with the observation fed back in the next prompt
            continue

        # Unknown shape → bail cleanly
        final_msg = "I’ll keep it simple: how can I help you with meds, contacts, places, or help points?"
        history.append({"role": "assistant", "content": final_msg})
        return {"ok": True, "model_used": model_used, "steps": actions, "final": final_msg}

    # If we hit max steps without a final
    safe_msg = "All set. If there’s anything else, tell me what you’d like to do next."
    history.append({"role": "assistant", "content": safe_msg})
    return {"ok": True, "steps": actions, "final": safe_msg}

# -----------------------------
# FastAPI shim
# -----------------------------
router = APIRouter(prefix="/api/agent", tags=["agent"])

@router.post("/chat")
def agent_chat(payload: Dict[str, Any] = Body(...)):
    """
    Body:
      {
        "user_id": 1,
        "message": "add a reminder...",
      }
    """
    user_id = int(payload.get("user_id", 1))
    message = str(payload.get("message", "")).strip()
    if not message:
        return JSONResponse({"ok": False, "error": "message required"}, status_code=400)
    out = agent_act(user_id=user_id, message=message)
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)

