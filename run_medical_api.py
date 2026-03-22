# run_medical_api.py
from fastapi import FastAPI, Query
from pydantic import BaseModel
from typing import Optional, List

from medical_record_tool import (
    save_medical_info,
    get_medical_info,
    log_medication_intake,
    add_doctor_appointment,
    list_upcoming_appointments,
)


app = FastAPI(
    title="Finding Dory – Medical Tools API",
    description="Shim endpoints that wrap medical_tools.py",
    version="1.0.0",
)

# ---- models for request bodies ----
class Medication(BaseModel):
    name: str
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    notes: Optional[str] = None

class SaveMedicalBody(BaseModel):
    user_id: int
    conditions: Optional[List[str]] = None
    medications: Optional[List[Medication]] = None
    allergies: Optional[List[str]] = None
    notes: Optional[str] = None
    merge: bool = True

class LogIntakeBody(BaseModel):
    user_id: int
    medication: str
    dose: Optional[str] = None
    taken_at_iso: Optional[str] = None
    notes: Optional[str] = None

class AddAppointmentBody(BaseModel):
    user_id: int
    doctor: str
    start_iso: str
    title: Optional[str] = None
    end_iso: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    status: str = "upcoming"  # upcoming|completed|canceled

# ---- endpoints ----
@app.post("/medical/save")
def http_save_medical(body: SaveMedicalBody):
    meds = [m.model_dump() for m in (body.medications or [])]
    return save_medical_info(
        user_id=body.user_id,
        conditions=body.conditions,
        medications=meds,
        allergies=body.allergies,
        notes=body.notes,
        merge=body.merge,
    )

@app.get("/medical/info")
def http_get_medical(
    user_id: int = Query(...),
    include_daily_log: bool = Query(False),
    upcoming_only: bool = Query(True),
    days_ahead: int = Query(365),
):
    return get_medical_info(
        user_id=user_id,
        include_daily_log=include_daily_log,
        upcoming_only=upcoming_only,
        days_ahead=days_ahead,
    )

@app.post("/medical/log")
def http_log_intake(body: LogIntakeBody):
    return log_medication_intake(
        user_id=body.user_id,
        medication=body.medication,
        dose=body.dose,
        taken_at_iso=body.taken_at_iso,
        notes=body.notes,
    )

@app.post("/medical/appointments/add")
def http_add_appt(body: AddAppointmentBody):
    return add_doctor_appointment(
        user_id=body.user_id,
        doctor=body.doctor,
        start_iso=body.start_iso,
        title=body.title,
        end_iso=body.end_iso,
        location=body.location,
        notes=body.notes,
        status=body.status,
    )

@app.get("/medical/appointments/upcoming")
def http_list_upcoming(user_id: int = Query(...), days_ahead: int = Query(365)):
    return list_upcoming_appointments(user_id=user_id, days_ahead=days_ahead)

# --- add to run_medical_api.py ---
from fastapi import Body
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from contact_tools import (
    add_contact, save_contacts, list_contacts, set_primary_contact,
    get_primary_contact, update_last_location, build_emergency_payload
)

class Contact(BaseModel):
    name: str
    phone: str
    relation: Optional[str] = None
    type: str = Field(default="emergency", description="emergency|caregiver|doctor|family|other")
    primary: bool = False
    notify_via: Optional[List[str]] = ["call"]
    notes: Optional[str] = None

class SaveContactsBody(BaseModel):
    user_id: int
    contacts: List[Contact]
    merge: bool = True

@app.post("/contacts/add", tags=["contacts"])
def http_add_contact(body: Contact = Body(...), user_id: int = Query(..., description="User ID")):
    """Add or upsert a single contact by phone."""
    return add_contact(user_id=user_id, **body.model_dump())

@app.post("/contacts/save", tags=["contacts"])
def http_save_contacts(body: SaveContactsBody):
    """Bulk save contacts (merge or replace)."""
    payload = [c.model_dump() for c in body.contacts]
    return save_contacts(user_id=body.user_id, contacts=payload, merge=body.merge)

@app.get("/contacts/list", tags=["contacts"])
def http_list_contacts(user_id: int = Query(...), kind: Optional[str] = Query(None),
                       relation: Optional[str] = Query(None), primary: Optional[bool] = Query(None)):
    """List contacts with optional filters."""
    return list_contacts(user_id=user_id, kind=kind, relation=relation, primary=primary)

@app.post("/contacts/primary/set", tags=["contacts"])
def http_set_primary(user_id: int = Query(...), contact_id: Optional[str] = Query(None), phone: Optional[str] = Query(None)):
    """Set the primary contact by contact_id or phone."""
    return set_primary_contact(user_id=user_id, contact_id=contact_id, phone=phone)

@app.get("/contacts/primary", tags=["contacts"])
def http_get_primary(user_id: int = Query(...)):
    """Get the primary (or first) contact."""
    return get_primary_contact(user_id=user_id) or {}

@app.post("/location/update", tags=["contacts"])
def http_update_location(user_id: int = Query(...), lat: float = Query(...), lng: float = Query(...),
                         timestamp_iso: Optional[str] = Query(None)):
    """Update the user's last known location in their JSON."""
    return update_last_location(user_id, lat, lng, timestamp_iso)

@app.get("/emergency/payload", tags=["contacts"])
def http_emergency_payload(user_id: int = Query(...), current_lat: Optional[float] = Query(None),
                           current_lng: Optional[float] = Query(None), address: Optional[str] = Query(None),
                           contact_id: Optional[str] = Query(None), phone_override: Optional[str] = Query(None),
                           message_prefix: str = Query("EMERGENCY: I need help.")):
    """Build tel/SMS/WhatsApp deep-links to contact + optional map link."""
    return build_emergency_payload(
        user_id=user_id,
        current_lat=current_lat,
        current_lng=current_lng,
        address=address,
        contact_id=contact_id,
        phone_override=phone_override,
        message_prefix=message_prefix
    )

# from fastapi import FastAPI, Query
# from pydantic import BaseModel
# from freq_places_tool import add_frequent_place_tool, get_frequent_places_tool, check_location

# app = FastAPI()

# class PlaceInput(BaseModel):
#     name: str
#     address: str
#     category: str
#     visit_frequency: str
#     notes: str | None = ""

# @app.post("/places/{user_id}")
# def add_place(user_id: int, place: PlaceInput):
#     return add_frequent_place_tool(user_id, place.name, place.address,
#                                    place.category, place.visit_frequency, place.notes)

# @app.get("/places/{user_id}")
# def get_places(user_id: int):
#     return get_frequent_places_tool(user_id)

# @app.get("/places/{user_id}/check")
# def check_place(user_id: int, address: str = Query(...)):
#     return check_location(user_id, address)