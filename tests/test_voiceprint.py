from datetime import UTC, datetime

from fastapi.testclient import TestClient

from newtalk.app import create_app
from newtalk.config import AppConfig
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.voiceprint import VoicePrintEnrollment, VoicePrintUnavailableError


class RecordingVoicePrintClient:
    def __init__(self) -> None:
        self.registered = []
        self.deleted = []

    async def register(self, *, device_id, identity_id, samples):
        self.registered.append((device_id, identity_id, samples))
        return VoicePrintEnrollment(identity_id, "test-model", datetime.now(UTC))

    async def delete(self, *, device_id, identity_id):
        self.deleted.append((device_id, identity_id))

    async def aclose(self):
        return None


class FailingVoicePrintClient(RecordingVoicePrintClient):
    async def register(self, **kwargs):
        raise VoicePrintUnavailableError("VoicePrint service is unavailable")


def make_client(voiceprint_client):
    app = create_app(
        AppConfig(),
        identity_service=IdentityService(InMemoryIdentityStore()),
        voiceprint_client=voiceprint_client,
    )
    client = TestClient(app)
    client.post("/api/device")
    member = client.post("/api/members", json={"display_name": "小明"}).json()
    return client, member


def samples(count=3):
    return [("samples", (f"sample-{index}.wav", b"wav-data", "audio/wav")) for index in range(count)]


def test_member_voiceprint_registration_and_deletion_are_device_scoped() -> None:
    provider = RecordingVoicePrintClient()
    client, member = make_client(provider)

    response = client.post(
        f"/api/members/{member['identity_id']}/voiceprint",
        files=samples(),
    )
    assert response.status_code == 200
    assert response.json()["model"] == "test-model"
    assert len(provider.registered[0][2]) == 3

    deleted = client.delete(f"/api/members/{member['identity_id']}/voiceprint")
    assert deleted.status_code == 204
    assert provider.deleted[0][1] == member["identity_id"]


def test_voiceprint_registration_rejects_wrong_sample_count_and_unknown_member() -> None:
    provider = RecordingVoicePrintClient()
    client, _ = make_client(provider)
    assert client.post("/api/members/missing/voiceprint", files=samples()).status_code == 404
    member = client.post("/api/members", json={"display_name": "小红"}).json()
    assert client.post(
        f"/api/members/{member['identity_id']}/voiceprint",
        files=samples(2),
    ).status_code == 422


def test_voiceprint_outage_does_not_break_chat_application() -> None:
    client, member = make_client(FailingVoicePrintClient())
    response = client.post(
        f"/api/members/{member['identity_id']}/voiceprint",
        files=samples(),
    )
    assert response.status_code == 503
    assert client.get("/health").status_code == 200

