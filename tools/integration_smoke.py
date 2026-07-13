import io

import httpx
from PIL import Image

BASE = "http://localhost:8000"


def token(email: str, password: str) -> str:
    response = httpx.post(f"{BASE}/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def main() -> None:
    user_token = token("user@example.com", "User123!")
    headers = {"Authorization": f"Bearer {user_token}"}
    composter = httpx.get(f"{BASE}/composters", headers=headers).json()[0]
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
    moderator = token("moderator@example.com", "Moderator123!")
    reviews = httpx.get(f"{BASE}/moderation/reviews", headers={"Authorization": f"Bearer {moderator}"}).json()
    assert reviews and reviews[0]["ml_status"] in {"LIKELY_VALID", "LIKELY_INVALID", "NEEDS_MANUAL_REVIEW"}
    print({"photo": uploaded.status_code, "review_id": reviews[0]["id"], "ml_status": reviews[0]["ml_status"], "confidence": reviews[0]["confidence"]})


if __name__ == "__main__":
    main()
