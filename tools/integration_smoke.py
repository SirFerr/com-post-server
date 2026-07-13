import io
import uuid

import httpx
from PIL import Image

BASE = "http://localhost:8000"


def token(email: str, password: str) -> str:
    response = httpx.post(f"{BASE}/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def main() -> None:
    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    registration = httpx.post(f"{BASE}/auth/register", json={"email": email, "password": "SmokeTest123!", "full_name": "Smoke Test"})
    registration.raise_for_status()
    user_token = registration.json()["access_token"]
    headers = {"Authorization": f"Bearer {user_token}"}
    profile = httpx.get(f"{BASE}/profile", headers=headers)
    profile.raise_for_status()
    assert profile.json()["email"] == email
    composters = httpx.get(f"{BASE}/composters", headers=headers).json()
    composter = next(item for item in composters if item["id"] == "00000000-0000-0000-0000-000000000001")
    access = httpx.post(
        f"{BASE}/composters/{composter['id']}/access",
        json={"latitude": composter["latitude"], "longitude": composter["longitude"]},
        headers=headers,
    ).json()
    httpx.post(
        f"{BASE}/sessions/{access['session_id']}/ack",
        json={"command_id": access["command"]["commandId"], "status": "SUCCESS"},
        headers=headers,
    ).raise_for_status()
    image = Image.new("RGB", (128, 128), (90, 130, 35))
    payload = io.BytesIO()
    image.save(payload, format="JPEG")
    uploaded = httpx.post(
        f"{BASE}/sessions/{access['session_id']}/photo",
        files={"file": ("organic.jpg", payload.getvalue(), "image/jpeg")},
        headers=headers,
    )
    uploaded.raise_for_status()
    upload_body = uploaded.json()
    httpx.post(
        f"{BASE}/sessions/{access['session_id']}/ack",
        json={"command_id": upload_body["command"]["commandId"], "status": "SUCCESS"},
        headers=headers,
    ).raise_for_status()
    moderator = token("moderator@example.com", "Moderator123!")
    reviews = httpx.get(f"{BASE}/moderation/reviews", headers={"Authorization": f"Bearer {moderator}"}).json()
    review = next(item for item in reviews if item["id"] == upload_body["review_id"])
    assert review["ml_status"] in {"LIKELY_VALID", "LIKELY_INVALID", "NEEDS_MANUAL_REVIEW"}
    assert review["user_email"] == email and review["composter_name"]
    httpx.post(
        f"{BASE}/moderation/reviews/{review['id']}",
        json={"approved": True, "comment": "Integration smoke"},
        headers={"Authorization": f"Bearer {moderator}"},
    ).raise_for_status()
    deposits = httpx.get(f"{BASE}/profile/deposits", headers=headers)
    deposits.raise_for_status()
    assert deposits.json()[0]["status"] == "CLOSED" and deposits.json()[0]["review_status"] == "APPROVED"
    print({"registration": registration.status_code, "photo": uploaded.status_code, "review_id": review["id"], "ml_status": review["ml_status"], "confidence": review["confidence"], "history": "CLOSED/APPROVED"})


if __name__ == "__main__":
    main()
