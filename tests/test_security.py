import hashlib
import hmac
import json

from app.models import Composter
from app.security import hash_password, signed_command, verify_device_response, verify_password


def test_password_hash_is_salted():
    first, second = hash_password("Password1!"), hash_password("Password1!")
    assert first != second
    assert verify_password("Password1!", first)
    assert not verify_password("wrong", first)


def test_command_signature_covers_payload():
    composter = Composter(id="c1", name="Test", device_id="d1", latitude=0, longitude=0, secret="secret")
    command = signed_command(composter, "s1", "OPEN")
    signature = command.pop("signature")
    canonical = json.dumps(command, separators=(",", ":"), sort_keys=True).encode()
    assert hmac.compare_digest(signature, hmac.new(b"secret", canonical, hashlib.sha256).hexdigest())
    assert command["expiresAt"] > command["issuedAt"]


def test_device_response_signature_covers_state_and_status():
    composter = Composter(id="c1", name="Test", device_id="d1", latitude=0, longitude=0, secret="secret")
    payload = {"commandId": "cmd", "deviceId": "d1", "state": "OPEN", "status": "SUCCESS", "v": 1}
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(b"secret", canonical, hashlib.sha256).hexdigest()
    assert verify_device_response(composter, "cmd", "SUCCESS", "OPEN", "d1", signature)
    assert not verify_device_response(composter, "cmd", "SUCCESS", "CLOSED", "d1", signature)
