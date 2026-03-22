"""
Enhanced claude_tool_runner.py for Dementia Assistant

Comprehensive tool suite that matches the backend functionality:
- Location tracking and safety monitoring
- Destination management with routing
- Emergency assistance
- Safety checks and reminders
- Memory support and guidance

Key improvements:
- Proactive safety monitoring
- Context-aware responses
- Emergency detection and response
- Natural conversation flow with actionable tools
"""

import re
import json
import time
import random
import requests
import boto3
from datetime import datetime, timedelta
from botocore.exceptions import ClientError
from typing import Dict, List, Optional, Tuple

# ---------- CONFIG ----------
REGION = "us-east-1"
MODEL_ID = "anthropic.claude-3-5-sonnet-20240620-v1:0"
BUCKET = "findingdoryuserdata"
BACKEND_BASE_URL = "http://localhost:8000"  # Your FastAPI backend

bedrock = boto3.client("bedrock-runtime", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)

# ---------- S3 helpers (enhanced) ----------
def get_user_json(user_id: int):
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
                "daily_checklist": {"completed": False, "date": None}
            }
        raise

def put_user_json(user_id: int, data: dict):
    key = f"users/{user_id}.json"
    data["last_updated"] = datetime.now().isoformat()
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json"
    )
    print(f"✅ S3 updated: {key}")

# ---------- Backend API Integration ----------
def call_backend_api(endpoint: str, params: dict = None, method: str = "GET"):
    """Call the FastAPI backend with error handling"""
    try:
        url = f"{BACKEND_BASE_URL}{endpoint}"
        if method == "GET":
            response = requests.get(url, params=params, timeout=10)
        elif method == "POST":
            response = requests.post(url, params=params, timeout=10)
        else:
            return {"ok": False, "error": f"Unsupported method: {method}"}
        
        if response.status_code == 200:
            return response.json()
        else:
            return {"ok": False, "error": f"Backend error: {response.status_code}"}
    except Exception as e:
        return {"ok": False, "error": f"Backend connection failed: {str(e)}"}

# ---------- Enhanced Tools ----------

def update_location_tool(user_id: int, latitude: float, longitude: float, 
                        battery_level: int = None, notes: str = None):
    """Update user location with safety monitoring"""
    # Call backend for enhanced location tracking
    backend_result = call_backend_api("/location/ping/enhanced", {
        "user_id": user_id,
        "lat": latitude,
        "lng": longitude,
        "battery_level": battery_level
    }, method="POST")
    
    # Update local storage
    data = get_user_json(user_id)
    data["last_location"] = {
        "lat": latitude,
        "lng": longitude,
        "timestamp": datetime.now().isoformat(),
        "battery_level": battery_level,
        "notes": notes
    }
    
    # Process safety alerts from backend
    alerts = []
    if backend_result.get("ok") and backend_result.get("basic_alerts"):
        alerts = backend_result["basic_alerts"]
        data["safety_alerts"] = alerts
    
    put_user_json(user_id, data)
    
    return {
        "ok": True,
        "location_updated": True,
        "safety_alerts": alerts,
        "recommendations": backend_result.get("recommendations", []),
        "destination_prompt": backend_result.get("destination_prompt")
    }

def get_navigation_to_destination(user_id: int, destination_name: str, 
                                current_lat: float, current_lng: float):
    """Get detailed navigation instructions to a saved destination"""
    result = call_backend_api("/api/destinations/start-navigation", {
        "user_id": user_id,
        "destination_name": destination_name,
        "current_lat": current_lat,
        "current_lng": current_lng
    }, method="POST")
    
    if result.get("ok"):
        navigation = result.get("navigation", {})
        return {
            "ok": True,
            "destination": navigation.get("destination", {}),
            "route_info": navigation.get("route_info", {}),
            "directions": navigation.get("text_directions", []),
            "safety_reminders": result.get("safety_reminders", []),
            "estimated_time": navigation.get("route_info", {}).get("estimated_time", "Unknown")
        }
    
    return result

def find_emergency_help(user_id: int, current_lat: float, current_lng: float, emergency_type: str = "general"):
    result = call_backend_api("/api/emergency/help-points", {
        "lat": current_lat, "lon": current_lng, "radius_m": 2000
    })
    if result.get("ok"):
        grouped = result.get("help_points", {}) or {}
        flat = []
        for pts in grouped.values():
            flat.extend(pts or [])
        flat.sort(key=lambda p: p.get("distance_m", 1_000_000))

        data = get_user_json(user_id)
        data.setdefault("emergency_history", []).append({
            "timestamp": datetime.now().isoformat(),
            "location": {"lat": current_lat, "lng": current_lng},
            "type": emergency_type,
            "help_points_found": len(flat),
        })
        put_user_json(user_id, data)

        return {
            "ok": True,
            "help_points": flat[:5],
            "emergency_numbers": result.get("emergency_numbers", {}),
            "emergency_message": result.get("emergency_message", ""),
            "immediate_action": "Stay calm, go to the nearest safe place listed below",
        }
    return result

def search_saved_destinations(user_id: int, query: str):
    """Search through user's saved destinations"""
    result = call_backend_api("/api/destinations/search", {
        "user_id": user_id,
        "query": query
    })
    
    if result.get("ok"):
        matches = result.get("matches", [])
        return {
            "ok": True,
            "matches": matches,
            "suggestion": result.get("suggestion"),
            "found_count": len(matches)
        }
    
    return result

def add_new_destination(user_id: int, name: str, address: str, category: str, 
                       notes: str = None, visit_frequency: str = "regular"):
    """Add a new destination to user's saved places"""
    result = call_backend_api("/api/destinations/add", {
        "user_id": user_id,
        "name": name,
        "address": address,
        "category": category,
        "notes": notes,
        "visit_frequency": visit_frequency
    }, method="POST")
    
    return result

def get_user_destinations(user_id: int):
    """Get all saved destinations for the user"""
    result = call_backend_api("/api/destinations/list", {
        "user_id": user_id
    })
    
    return result

def check_if_user_safe(user_id: int):
    """Comprehensive safety status check"""
    data = get_user_json(user_id)
    
    # Check last location update
    last_location = data.get("last_location")
    if not last_location:
        return {
            "ok": False,
            "status": "unknown",
            "message": "No recent location data available"
        }
    
    last_update = datetime.fromisoformat(last_location["timestamp"])
    time_since_update = datetime.now() - last_update
    
    # Check for concerning patterns
    concerns = []
    
    if time_since_update > timedelta(hours=2):
        concerns.append("No location update for over 2 hours")
    
    if last_location.get("battery_level", 100) < 15:
        concerns.append("Phone battery critically low")
    
    safety_alerts = data.get("safety_alerts", [])
    if "left_home" in safety_alerts:
        concerns.append("User has left their home area")
    
    if "unusual_route" in safety_alerts:
        concerns.append("User is in an unfamiliar area")
    
    status = "safe" if not concerns else "needs_attention"
    
    return {
        "ok": True,
        "status": status,
        "last_location": last_location,
        "concerns": concerns,
        "time_since_update": str(time_since_update),
        "recommendations": [
            "Contact user to check their wellbeing" if concerns else "User appears to be safe",
            "Review recent location history for patterns" if len(concerns) > 1 else None
        ]
    }

def provide_memory_assistance(user_id: int, memory_type: str, query: str = None):
    """Provide memory assistance and reminders"""
    data = get_user_json(user_id)
    
    if memory_type == "medications":
        return {
            "ok": True,
            "type": "medication_reminder",
            "message": "Remember to take your daily medications",
            "reminder": "Check your pill organizer or medication list",
            "next_action": "Mark this reminder as completed when done"
        }
    
    elif memory_type == "appointments":
        return {
            "ok": True,
            "type": "appointment_reminder", 
            "message": "Do you have any appointments today?",
            "reminder": "Check your calendar or appointment card",
            "suggestion": "Call the clinic if you're unsure about appointment times"
        }
    
    elif memory_type == "people":
        return {
            "ok": True,
            "type": "people_reminder",
            "message": "If you're trying to remember someone, describe what you recall",
            "suggestion": "Look at your emergency contacts list or recent calls"
        }
    
    elif memory_type == "routine":
        current_hour = datetime.now().hour
        if 6 <= current_hour < 12:
            routine = "Morning routine: Take medications, eat breakfast, check the weather"
        elif 12 <= current_hour < 17:
            routine = "Afternoon: Lunch time, possible nap, light activities"
        elif 17 <= current_hour < 21:
            routine = "Evening: Dinner, family time, prepare for tomorrow"
        else:
            routine = "Night time: Prepare for bed, ensure doors are locked"
        
        return {
            "ok": True,
            "type": "routine_guidance",
            "message": f"Current time routine guidance: {routine}",
            "time": datetime.now().strftime("%H:%M")
        }
    
    return {
        "ok": True,
        "type": "general_memory_support",
        "message": "I'm here to help with your memory needs",
        "available_assistance": ["medications", "appointments", "people", "routine", "places"]
    }

# ---------- Bedrock safe wrapper (unchanged) ----------
import botocore

def safe_converse(**kwargs):
    """Call bedrock.converse with retries/backoff for throttling."""
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            return bedrock.converse(**kwargs)
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ThrottlingException" or "Throttling" in str(e):
                wait = (2 ** attempt) + random.random()
                print(f"⏳ Throttled, retrying in {wait:.2f}s...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Exceeded retries for bedrock.converse")

# ---------- Enhanced Tool Definitions ----------
TOOLS_DEF_FOR_BEDROCK = [
    {
        "toolSpec": {
            "name": "update_location",
            "description": "Update user's current location and perform safety checks",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "latitude": {"type": "number"},
                        "longitude": {"type": "number"},
                        "battery_level": {"type": "integer"},
                        "notes": {"type": "string"}
                    },
                    "required": ["user_id", "latitude", "longitude"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "get_navigation",
            "description": "Get turn-by-turn navigation to a saved destination",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "destination_name": {"type": "string"},
                        "current_lat": {"type": "number"},
                        "current_lng": {"type": "number"}
                    },
                    "required": ["user_id", "destination_name", "current_lat", "current_lng"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "find_emergency_help",
            "description": "Find nearby emergency help points (hospitals, police, MRT stations)",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "current_lat": {"type": "number"},
                        "current_lng": {"type": "number"},
                        "emergency_type": {"type": "string"}
                    },
                    "required": ["user_id", "current_lat", "current_lng"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "safety_checklist",
            "description": "Trigger safety checklist (leaving home, emergency, routine)",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "check_type": {"type": "string", "enum": ["leaving_home", "emergency", "routine"]}
                    },
                    "required": ["user_id", "check_type"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "search_destinations",
            "description": "Search through user's saved destinations by name or category",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "query": {"type": "string"}
                    },
                    "required": ["user_id", "query"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "add_destination",
            "description": "Add a new destination to user's saved places",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "name": {"type": "string"},
                        "address": {"type": "string"},
                        "category": {"type": "string", "enum": ["home", "family", "shopping", "medical", "recreation"]},
                        "notes": {"type": "string"},
                        "visit_frequency": {"type": "string", "enum": ["daily", "weekly", "monthly", "regular"]}
                    },
                    "required": ["user_id", "name", "address", "category"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "get_destinations",
            "description": "Get list of all saved destinations for the user",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"}
                    },
                    "required": ["user_id"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "check_safety_status",
            "description": "Check comprehensive safety status of the user",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"}
                    },
                    "required": ["user_id"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "memory_assistance",
            "description": "Provide memory assistance (medications, appointments, people, routine)",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "memory_type": {"type": "string", "enum": ["medications", "appointments", "people", "routine"]},
                        "query": {"type": "string"}
                    },
                    "required": ["user_id", "memory_type"]
                }
            }
        }
    }
]

# ---------- Enhanced System Prompt ----------
SYSTEM_PROMPT = '''You are a caring, intelligent dementia assistant named Dory. Your primary goals are:

1. SAFETY FIRST: Always prioritize the user's safety and wellbeing
2. PROACTIVE CARE: Anticipate needs and offer help before being asked
3. MEMORY SUPPORT: Help with memory issues with patience and understanding
4. CLEAR COMMUNICATION: Use simple, clear language and repeat important information

BEHAVIOR GUIDELINES:
- Be warm, patient, and encouraging
- Break complex tasks into simple steps
- Always confirm understanding before proceeding
- Offer specific, actionable help
- Stay calm in emergency situations
- Remember that confusion and repetition are normal

TOOL USAGE:
- Use tools proactively to help the user
- When location is mentioned, always update_location first
- For navigation requests, get current location then provide navigation
- For emergencies, immediately find_emergency_help
- Regularly check_safety_status for users who seem confused
- Provide memory_assistance when users seem forgetful

RESPONSE PATTERN:
1. Use appropriate tools based on user needs
2. Provide clear, actionable information
3. Offer additional help or next steps
4. Include safety reminders when relevant
5. Keep responses warm but concise

Remember: You're not just answering questions - you're actively helping someone stay safe and independent.'''

# ---------- Enhanced parsing helpers (updated for new tools) ----------
def extract_text_from_response(resp):
    """Extract assistant text from Bedrock response"""
    out = resp.get("output", {})
    try:
        msg = out.get("message")
        if msg and isinstance(msg, dict):
            contents = msg.get("content", [])
            if isinstance(contents, list):
                texts = []
                for c in contents:
                    if isinstance(c, dict) and "text" in c:
                        texts.append(c["text"])
                    elif isinstance(c, str):
                        texts.append(c)
                if texts:
                    return "\n".join(texts)
    except Exception:
        pass

    try:
        return out["message"]["content"][0]["text"]
    except Exception:
        pass

    return json.dumps(out)

def parse_toolcall_from_response(resp):
    """Parse tool call from Bedrock response with enhanced patterns"""
    out = resp.get("output", {})
    
    # Official toolUse field
    tooluse = out.get("toolUse") or out.get("toolsUsed") or out.get("toolCalls")
    if tooluse:
        if isinstance(tooluse, list) and len(tooluse) > 0:
            tu = tooluse[0]
            return {"name": tu.get("name"), "input": tu.get("input", {})}
        elif isinstance(tooluse, dict):
            return {"name": tooluse.get("name"), "input": tooluse.get("input", {})}

    # Parse from assistant text
    text = extract_text_from_response(resp)
    if text:
        # Enhanced patterns for new tools
        patterns = [
            (r'update.{0,10}location', 'update_location'),
            (r'navigation|directions|how to get', 'get_navigation'),
            (r'emergency|help|urgent', 'find_emergency_help'),
            (r'safety.{0,10}check|checklist', 'safety_checklist'),
            (r'search.{0,10}destination', 'search_destinations'),
            (r'add.{0,10}destination|save.{0,10}place', 'add_destination'),
            (r'list.{0,10}destination|show.{0,10}places', 'get_destinations'),
            (r'check.{0,10}safety|am I safe', 'check_safety_status'),
            (r'memory|remember|forgot', 'memory_assistance')
        ]
        
        text_lower = text.lower()
        for pattern, tool_name in patterns:
            if re.search(pattern, text_lower):
                return {"name": tool_name, "input": {"user_id": 1}}
    
    return None

# ---------- Enhanced Main Runner ----------
def run_tool_by_name(name: str, inputs: dict):
    """Execute tool by name with error handling"""
    try:
        if name == "update_location":
            return update_location_tool(**inputs)
        elif name == "get_navigation":
            return get_navigation_to_destination(**inputs)
        elif name == "find_emergency_help":
            return find_emergency_help(**inputs)
        elif name == "search_destinations":
            return search_saved_destinations(**inputs)
        elif name == "add_destination":
            return add_new_destination(**inputs)
        elif name == "get_destinations":
            return get_user_destinations(**inputs)
        elif name == "check_safety_status":
            return check_if_user_safe(**inputs)
        elif name == "memory_assistance":
            return provide_memory_assistance(**inputs)
        else:
            return {"ok": False, "error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"ok": False, "error": f"Tool execution failed: {str(e)}"}

def run_claude_with_enhanced_tools(user_message: str, user_id: int = 1):
    """Enhanced orchestration with comprehensive tool support"""
    try:
        resp = safe_converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            toolConfig={"tools": TOOLS_DEF_FOR_BEDROCK},
        )
    except Exception as e:
        print(f"Bedrock error: {e}")
        return "I'm having trouble connecting to my systems. Please try again in a moment, or if this is an emergency, call 995 immediately."

    # Parse tool call
    tool_call = parse_toolcall_from_response(resp)
    assistant_text = extract_text_from_response(resp)

    if not tool_call:
        return assistant_text.strip() if assistant_text else "I'm here to help. What do you need assistance with?"

    # Execute tool
    name = tool_call.get("name")
    inputs = tool_call.get("input", {})
    
    # Ensure user_id is set
    if "user_id" not in inputs:
        inputs["user_id"] = user_id

    tool_result = run_tool_by_name(name, inputs)
    
    # Generate contextual response based on tool result
    if not tool_result.get("ok"):
        return f"I encountered an issue: {tool_result.get('error', 'Unknown error')}. Let me know if you need help with something else."
    
    # Return assistant's response, enriched with tool results
    base_response = assistant_text.strip() if assistant_text else ""
    
    # Add specific enhancements based on tool type
    if name == "find_emergency_help" and tool_result.get("help_points"):
        help_points = tool_result["help_points"][:3]
        locations_text = "\n".join([f"• {pt['name']} - {pt['distance_m']}m away" for pt in help_points])
        return f"{base_response}\n\nNearest help locations:\n{locations_text}\n\nFor immediate emergency, call 995 (ambulance) or 999 (police)."
    
    elif name == "get_navigation" and tool_result.get("directions"):
        directions = tool_result["directions"][:5]  # First 5 steps
        directions_text = "\n".join([f"{i+1}. {step}" for i, step in enumerate(directions)])
        time_estimate = tool_result.get("estimated_time", "")
        return f"{base_response}\n\nNavigation ({time_estimate}):\n{directions_text}\n\nStay safe and take your time!"
    
    return base_response if base_response else "Task completed successfully. How else can I help you today?"

# ---------- Example usage ----------
# Replace the bottom section of your claude_tool_runner.py with this:

if __name__ == "__main__":
    print("=" * 50)
    print("DEMENTIA ASSISTANT - DORY")
    print("=" * 50)
    print("Hello! I'm Dory, your caring dementia assistant.")
    print("I can help you with:")
    print("- Navigation and directions")
    print("- Finding help in emergencies") 
    print("- Memory reminders")
    print("- Safety checks")
    print("- Managing your saved places")
    print("- Or just chat about anything!")
    print()
    print("Type 'quit' or 'exit' to end our conversation")
    print("=" * 50)
    
    user_id = 1  # You can change this for different users
    
    while True:
        try:
            user_input = input("\nYou: ").strip()
            
            # Exit commands
            if user_input.lower() in ['quit', 'exit', 'q', 'goodbye', 'bye']:
                print("\nDory: Take care and stay safe! I'm here whenever you need me.")
                break
            
            # Skip empty inputs
            if not user_input:
                continue
                
            # Special commands for testing
            if user_input.lower() == 'help':
                print("\nDory: I can help you with many things! Try saying:")
                print("- 'I'm at [location]' - to update your location")
                print("- 'How do I get to [place]?' - for navigation")
                print("- 'I'm lost' - for emergency help")
                print("- 'Did I take my medicine?' - for memory help")
                print("- 'Add [place] to my destinations' - to save places")
                print("- 'Show my places' - to see saved destinations")
                continue
                
            if user_input.lower() == 'test':
                print("\nDory: Running a quick test...")
                user_input = "I'm at Marina Bay Sands"
            
            print("\nDory: ", end="", flush=True)  # Show that Dory is thinking
            
            # Get response from enhanced Claude
            response = run_claude_with_enhanced_tools(user_input, user_id)
            
            # Clean up and display response
            response = response.strip()
            if not response:
                response = "I'm here to help! What would you like to do?"
                
            print(response)
            
        except KeyboardInterrupt:
            print("\n\nDory: Goodbye! Stay safe!")
            break
        except Exception as e:
            print(f"\nDory: I'm having a technical issue right now. Error: {str(e)}")
            print("Please try again, or if this is urgent, contact emergency services.") 