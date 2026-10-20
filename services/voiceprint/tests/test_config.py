from newtalk_voiceprint.config import VoicePrintConfig


def test_config_loads_dotenv_and_reuses_main_service_values(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "NEWTALK_DATABASE_URL=postgresql+asyncpg://user:pass@db/newtalk\n"
        "NEWTALK_VOICEPRINT_API_TOKEN=shared-token\n",
        encoding="utf-8",
    )
    for name in (
        "VOICEPRINT_DATABASE_URL",
        "VOICEPRINT_API_TOKEN",
        "NEWTALK_DATABASE_URL",
        "NEWTALK_VOICEPRINT_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    config = VoicePrintConfig.from_environment()

    assert config.database_url == "postgresql+asyncpg://user:pass@db/newtalk"
    assert config.api_token == "shared-token"


def test_voiceprint_specific_environment_takes_precedence(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "NEWTALK_DATABASE_URL",
        "postgresql+asyncpg://main:pass@db/newtalk",
    )
    monkeypatch.setenv("NEWTALK_VOICEPRINT_API_TOKEN", "main-token")
    monkeypatch.setenv(
        "VOICEPRINT_DATABASE_URL",
        "postgresql+asyncpg://voice:pass@db/newtalk",
    )
    monkeypatch.setenv("VOICEPRINT_API_TOKEN", "voice-token")

    config = VoicePrintConfig.from_environment()

    assert config.database_url == "postgresql+asyncpg://voice:pass@db/newtalk"
    assert config.api_token == "voice-token"
