from fastapi import FastAPI, Query
from pydantic import BaseModel
from tools.freq_places_tool import add_frequent_place_tool, get_frequent_places_tool, check_location

app = FastAPI()

class PlaceInput(BaseModel):
    name: str
    address: str
    category: str
    visit_frequency: str
    notes: str | None = ""

@app.post("/places/{user_id}")
def add_place(user_id: int, place: PlaceInput):
    return add_frequent_place_tool(user_id, place.name, place.address,
                                   place.category, place.visit_frequency, place.notes)

@app.get("/places/{user_id}")
def get_places(user_id: int):
    return get_frequent_places_tool(user_id)

@app.get("/places/{user_id}/check")
def check_place(user_id: int, address: str = Query(...)):
    return check_location(user_id, address)