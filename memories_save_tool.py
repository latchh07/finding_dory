"""
Memory Save Tool for Finding Dory 

This module manages user memory triggers (people, events, images) for dementia 
support. It stores metadata in S3 (JSON files) and images in S3 buckets with 
presigned URLs for safe temporary access.

Core features:
- Add new memory triggers (name, description, image)
- Retrieve memory triggers (with presigned URLs for frontend access)
- Search for memories by person name
- Track mistakes in recall and sum them at person-level for notifications

S3 Structure:
- User data:   s3://findingdoryuserdata/users/{user_id}.json
- Memory imgs: s3://findingdoryuserdata/finding_dory/memories/{user_id}/...

JSON Structure (per user):
{
  "user_id": 1,
  "frequent_places": [],
  "memory_triggers": [
      {
        "name": "Alice",
        "description": "Alice at her birthday party",
        "image": "finding_dory/memories/1/Alice_20250906123000.png",
        "added_at": "2025-09-06T12:30:00",
        "mistakes": 0
      }
  ],
  "last_location": null,
  "safety_alerts": [],
  "daily_checklist": {"completed": false, "date": null},
  "last_updated": "2025-09-06T12:34:56"
}
"""

import os
import json
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# ---------- CONFIG ----------
REGION = "us-east-1"
BUCKET = "findingdoryuserdata"
IMAGE_FOLDER = "finding_dory/memories"
PRESIGNED_EXPIRY = 52 * 3600  # 52 hours

s3 = boto3.client("s3", region_name=REGION)

# ---------- JSON helpers ----------
def get_user_json(user_id: int):
    """
    Retrieve user JSON profile from S3.
    If not found, return a default structure.

    Args:
        user_id (int): ID of the user.

    Returns:
        dict: User JSON data with keys like memory_triggers, frequent_places, etc.
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
                "daily_checklist": {"completed": False, "date": None}
            }
        raise


def put_user_json(user_id: int, data: dict):
    """
    Save or update user JSON profile to S3.

    Args:
        user_id (int): ID of the user.
        data (dict): User JSON data.

    Side Effects:
        Updates the user's JSON file in the S3 bucket.
    """
    key = f"users/{user_id}.json"
    data["last_updated"] = datetime.now().isoformat()
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json"
    )
    print(f"✅ S3 updated: {key}")


# ---------- S3 helpers ----------
def upload_image_to_s3(local_file_path: str, user_id: int, person_name: str):
    """
    Upload an image to S3 for a specific user and person.

    Args:
        local_file_path (str): Local path of the image file.
        user_id (int): ID of the user.
        person_name (str): Name of the person in the memory.

    Returns:
        str: S3 key of the uploaded image.
    """
    ext = os.path.splitext(local_file_path)[-1]
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    s3_key = f"{IMAGE_FOLDER}/{user_id}/{person_name}_{timestamp}{ext}"
    s3.upload_file(local_file_path, BUCKET, s3_key)
    print(f"✅ Image uploaded: {s3_key}")
    return s3_key


def generate_presigned_url(s3_key: str, expiry: int = PRESIGNED_EXPIRY):
    """
    Generate a temporary presigned URL for an S3 object.

    Args:
        s3_key (str): Key of the S3 object.
        expiry (int): Expiration time in seconds (default: 52 hours).

    Returns:
        str: Presigned URL for accessing the S3 object.
    """
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": s3_key},
        ExpiresIn=expiry
    )


# ---------- Memory Tool ----------
def add_memory_trigger(user_id: int, person_name: str, description: str, local_image_path: str):
    """
    Add a new memory trigger for a user, including uploading the image to S3.

    Args:
        user_id (int): ID of the user.
        person_name (str): Name of the person in the memory.
        description (str): Description of the memory.
        local_image_path (str): Local path to the image.

    Returns:
        dict: Result with S3 key and presigned URL.
    """
    s3_key = upload_image_to_s3(local_image_path, user_id, person_name)
    data = get_user_json(user_id)

    memory_entry = {
        "name": person_name,
        "description": description,
        "image": s3_key,
        "added_at": datetime.now().isoformat(),
        "mistakes": 0
    }
    data.setdefault("memory_triggers", []).append(memory_entry)
    put_user_json(user_id, data)

    url = generate_presigned_url(s3_key)
    return {"ok": True, "s3_key": s3_key, "url": url}


def get_user_memory_with_urls(user_id: int):
    """
    Retrieve all memory triggers for a user, attaching presigned URLs.

    Args:
        user_id (int): ID of the user.

    Returns:
        list: List of memory dictionaries with presigned URLs.
    """
    data = get_user_json(user_id)
    memories = data.get("memory_triggers", [])
    
    for mem in memories:
        if "image" in mem and mem["image"]:
            mem["url"] = generate_presigned_url(mem["image"])
        else:
            mem["url"] = None
    return memories


def get_memory_by_name(user_id: int, person_name: str):
    """
    Search memory triggers by person name.

    Args:
        user_id (int): ID of the user.
        person_name (str): Person's name to search for.

    Returns:
        list: List of memory entries matching the name.
    """
    memories = get_user_memory_with_urls(user_id)
    results = [m for m in memories if person_name.lower() in m["name"].lower()]
    return results


def increment_memory_mistake(user_id: int, person_name: str, description: str):
    """
    Increment the mistake counter for a specific memory.

    Args:
        user_id (int): ID of the user.
        person_name (str): Name of the person in the memory.
        description (str): Description of the memory.

    Returns:
        dict: Updated mistake count for the memory.
    """
    data = get_user_json(user_id)
    for mem in data.get("memory_triggers", []):
        if mem["name"] == person_name and mem["description"] == description:
            mem["mistakes"] = mem.get("mistakes", 0) + 1
            break
    put_user_json(user_id, data)
    return {"ok": True, "person_name": person_name, "description": description, "mistakes": mem["mistakes"]}


def get_total_mistakes_for_person(user_id: int, person_name: str) -> int:
    """
    Calculate the total number of mistakes made for a specific person across all memories.

    Args:
        user_id (int): ID of the user.
        person_name (str): Person's name.

    Returns:
        int: Total mistakes count for that person.
    """
    data = get_user_json(user_id)
    return sum(mem.get("mistakes", 0) for mem in data.get("memory_triggers", []) if mem["name"] == person_name)