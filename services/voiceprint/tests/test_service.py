from datetime import UTC, datetime
from io import BytesIO
import asyncio
import logging
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from newtalk_voiceprint.audio import InvalidAudioError
from newtalk_voiceprint.app import create_app
from newtalk_voiceprint.config import VoicePrintConfig
from newtalk_voiceprint.embedder import DeterministicEmbedder
from newtalk_voiceprint.service import IdentityNotFoundError, VoicePrintService
from newtalk_voiceprint.store import StoredTemplate


def wav_tone(frequency: float, seconds: float = 3.0) -> bytes:
    sample_rate = 16_000
    time = np.arange(int(sample_rate * seconds), dtype=np.float32) / sample_rate
    samples = (np.sin(2 * np.pi * frequency * time) * 12_000).astype("<i2")
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(samples.tobytes())
    return output.getvalue()


class MemoryStore:
    def __init__(self) -> None:
        self.identities = {("family-a", "member-a"), ("family-b", "member-b")}
        self.templates: dict[tuple[str, str], np.ndarray] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ping(self) -> None:
        return None

    async def save_template(self, *, device_id, identity_id, embedding, model_name):
        if (device_id, identity_id) not in self.identities:
            return None
        self.templates[(device_id, identity_id)] = embedding
        return datetime.now(UTC)

    async def list_templates(self, device_id, *, model_name):
        return [
            StoredTemplate(identity_id, embedding)
            for (family, identity_id), embedding in self.templates.items()
            if family == device_id
        ]

    async def delete_template(self, *, device_id, identity_id):
        return self.templates.pop((device_id, identity_id), None) is not None


def make_service(store: MemoryStore | None = None) -> VoicePrintService:
    return VoicePrintService(
        store or MemoryStore(),
        DeterministicEmbedder(),
        threshold=0.9,
        min_sample_seconds=2,
        identify_min_sample_seconds=0.8,
        max_sample_seconds=10,
        max_audio_bytes=500_000,
    )


def test_default_similarity_threshold_is_half() -> None:
    assert VoicePrintConfig().backend == "campplus"
    assert VoicePrintConfig().similarity_threshold == 0.5
    assert VoicePrintConfig().identify_min_sample_seconds == 0.8


def test_three_samples_register_identify_scope_and_delete() -> None:
    async def scenario() -> None:
        store = MemoryStore()
        service = make_service(store)
        sample = wav_tone(220)
        enrolled_at = await service.register(
            device_id="family-a",
            identity_id="member-a",
            samples=[sample, sample, sample],
        )

        assert enrolled_at.tzinfo is not None
        assert (await service.identify(device_id="family-a", sample=sample)).identity_id == "member-a"
        assert (await service.identify(device_id="family-b", sample=sample)).matched is False
        assert await service.delete(device_id="family-a", identity_id="member-a") is True
        assert (await service.identify(device_id="family-a", sample=sample)).matched is False

    asyncio.run(scenario())


def test_identification_logs_score_threshold_and_elapsed(caplog) -> None:
    async def scenario() -> None:
        store = MemoryStore()
        service = make_service(store)
        sample = wav_tone(220)
        await service.register(
            device_id="family-a",
            identity_id="member-a",
            samples=[sample, sample, sample],
        )
        with caplog.at_level(logging.INFO, logger="newtalk_voiceprint.service"):
            await service.identify(device_id="family-a", sample=sample)

    asyncio.run(scenario())
    message = next(
        record.getMessage()
        for record in caplog.records
        if "voiceprint_identify_completed" in record.getMessage()
    )
    assert "templates=1" in message
    assert "score=" in message
    assert "threshold=0.9000" in message
    assert "matched=True" in message
    assert "elapsed_ms=" in message


def test_registration_rejects_wrong_count_invalid_audio_and_foreign_member() -> None:
    async def scenario() -> None:
        service = make_service()
        sample = wav_tone(220)
        with pytest.raises(ValueError, match="three"):
            await service.register(device_id="family-a", identity_id="member-a", samples=[sample])
        with pytest.raises(InvalidAudioError):
            await service.register(
                device_id="family-a",
                identity_id="member-a",
                samples=[b"not-wave"] * 3,
            )
        with pytest.raises(IdentityNotFoundError):
            await service.register(
                device_id="family-a",
                identity_id="member-b",
                samples=[sample] * 3,
            )

    asyncio.run(scenario())


def test_http_api_requires_token_and_supports_register_identify_delete() -> None:
    config = VoicePrintConfig(api_token="test-token", backend="deterministic")
    service = make_service()
    app = create_app(config, service=service)
    headers = {"Authorization": "Bearer test-token"}
    sample = wav_tone(220)
    files = [("samples", (f"sample-{index}.wav", sample, "audio/wav")) for index in range(3)]

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["backend"] == "deterministic"
        assert health.json()["model"] == "deterministic-test-v1"
        assert client.post("/v1/templates").status_code == 401
        registered = client.post(
            "/v1/templates",
            headers=headers,
            data={"device_id": "family-a", "identity_id": "member-a"},
            files=files,
        )
        assert registered.status_code == 200
        identified = client.post(
            "/v1/identify",
            headers=headers,
            data={"device_id": "family-a"},
            files={"sample": ("probe.wav", sample, "audio/wav")},
        )
        assert identified.json()["identity_id"] == "member-a"
        assert client.delete(
            "/v1/templates/member-a",
            headers=headers,
            params={"device_id": "family-a"},
        ).status_code == 200


def test_http_identify_logs_short_audio_rejection(caplog) -> None:
    config = VoicePrintConfig(api_token="test-token", backend="deterministic")
    app = create_app(config, service=make_service())
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client, caplog.at_level(
        logging.WARNING,
        logger="newtalk_voiceprint.app",
    ):
        response = client.post(
            "/v1/identify",
            headers=headers,
            data={"device_id": "family-a"},
            files={"sample": ("short.wav", wav_tone(220, seconds=0.5), "audio/wav")},
        )

    assert response.status_code == 422
    assert "between 0.8 and 10 seconds" in response.json()["detail"]
    assert any(
        "voiceprint_identify_rejected" in record.getMessage()
        and "between 0.8 and 10 seconds" in record.getMessage()
        for record in caplog.records
    )


def test_identification_accepts_audio_shorter_than_enrollment_minimum() -> None:
    async def scenario() -> None:
        service = make_service()
        enrolled_sample = wav_tone(220, seconds=3)
        await service.register(
            device_id="family-a",
            identity_id="member-a",
            samples=[enrolled_sample] * 3,
        )

        result = await service.identify(
            device_id="family-a",
            sample=wav_tone(220, seconds=1),
        )

        assert result.identity_id == "member-a"
        assert result.matched is True

    asyncio.run(scenario())
