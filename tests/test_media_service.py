import io
from datetime import datetime, timezone

import httpx
from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image

import media_service
from app.domain import media_client, storage


def image_bytes() -> bytes:
    image = Image.frombytes("RGB", (32, 32), bytes((index * 37) % 256 for index in range(32 * 32 * 3)))
    result = io.BytesIO()
    image.save(result, format="JPEG")
    return result.getvalue()


def test_media_service_owns_photo_upload_and_metadata(monkeypatch):
    monkeypatch.setenv("MEDIA_INTERNAL_TOKEN", "test-internal-token")

    class FakeStorage:
        def __init__(self):
            self.objects = {}

        def put_object(self, *, Bucket, Key, Body, ContentType):
            self.objects[Key] = (Body, ContentType)

        def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, **_):
            return {"Contents": [
                {"Key": key, "Size": len(payload), "LastModified": datetime(2026, 9, 19, tzinfo=timezone.utc)}
                for key, (payload, _) in self.objects.items() if key.startswith(Prefix)
            ][:MaxKeys]}

        def head_object(self, *, Bucket, Key):
            payload, content_type = self.objects[Key]
            return {"ContentLength": len(payload), "ContentType": content_type, "ETag": '"abc"'}

        def generate_presigned_url(self, operation, *, Params, ExpiresIn):
            return f"http://example.test/{Params['Key']}?expires={ExpiresIn}"

    fake = FakeStorage()
    monkeypatch.setattr(media_service, "_storage", lambda public=False: fake)
    client = TestClient(media_service.app)
    photo = image_bytes()
    assert client.post("/photos", files={"file": ("proof.jpg", photo, "image/jpeg")}).status_code == 403
    headers = {"X-Internal-Token": "test-internal-token"}
    assert client.post("/photos?namespace=ml/active", files={"file": ("proof.jpg", photo, "image/jpeg")}, headers=headers).status_code == 422
    uploaded = client.post("/photos", files={"file": ("proof.jpg", photo, "image/jpeg")}, headers=headers)
    assert uploaded.status_code == 201
    key = uploaded.json()["key"]
    assert key.startswith("deposits/")
    assert fake.objects[key] == (photo, "image/jpeg")
    listed = client.get("/objects", headers=headers).json()
    assert listed["objects"][0]["key"] == key
    assert client.get("/objects/metadata", params={"key": key}, headers=headers).json()["size"] == len(photo)
    assert client.get("/objects/url", params={"key": key}, headers=headers).json()["url"].startswith("http://example.test/")


def test_photo_caller_uses_media_contract(monkeypatch):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"key": "deposits/test.jpg"} if method == "POST" else {"url": "http://example.test/test.jpg"}

    monkeypatch.setattr(media_client, "_request", fake_request)
    monkeypatch.setattr(storage, "presigned_url", media_client.presigned_url)
    photo = UploadFile(filename="proof.jpg", file=io.BytesIO(image_bytes()))
    # storage imported the client methods directly, so their underlying HTTP call is exercised.
    assert storage.store_photo(photo) == "deposits/test.jpg"
    assert storage.photo_url("deposits/test.jpg") == "http://example.test/test.jpg"
    assert calls[0][1] == "/photos"
    assert calls[0][2]["params"] == {"namespace": "deposits"}
    assert calls[1][2]["params"] == {"key": "deposits/test.jpg"}


def test_media_client_authenticates_internal_http_call(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"url": "http://example.test/photo.jpg"})

    actual_client = httpx.Client
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(media_client.httpx, "Client", lambda **kwargs: actual_client(transport=transport, **kwargs))
    assert media_client.presigned_url("deposits/photo.jpg") == "http://example.test/photo.jpg"
    assert seen[0].headers["X-Internal-Token"] == media_client.get_settings().media_internal_token
    assert seen[0].url.params["key"] == "deposits/photo.jpg"


def test_storage_adapter_restores_datetime_from_http(monkeypatch):
    monkeypatch.setattr(media_client, "list_media_objects", lambda *args: {
        "objects": [{"key": "deposits/photo.jpg", "size": 4, "modified": "2026-09-19T09:00:00+00:00"}],
        "cursor": None,
    })
    monkeypatch.setattr(media_client, "media_object", lambda key: {
        "size": 4, "modified": "2026-09-19T09:00:00Z", "content_type": "image/jpeg",
    })
    adapter = media_client.storage_client()
    listed = adapter.list_objects_v2(Bucket="photos")["Contents"][0]
    headed = adapter.head_object(Bucket="photos", Key="deposits/photo.jpg")
    assert listed["LastModified"].strftime("%d.%m.%Y") == "19.09.2026"
    assert headed["LastModified"].tzinfo == timezone.utc
