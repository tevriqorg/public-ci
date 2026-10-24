from transfer_hub.config import Settings


def test_stage_concurrency_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("P20_TRANSFER_DATA", str(tmp_path))
    settings = Settings.from_env()
    assert settings.fetch_concurrency == 2
    assert settings.process_concurrency == 1
    assert settings.delivery_concurrency == 1
    assert settings.stage_lease_seconds == 60


def test_stage_concurrency_env_is_positive(monkeypatch, tmp_path):
    monkeypatch.setenv("P20_TRANSFER_DATA", str(tmp_path))
    monkeypatch.setenv("P20_FETCH_CONCURRENCY", "4")
    monkeypatch.setenv("P20_PROCESS_CONCURRENCY", "0")
    monkeypatch.setenv("P20_DELIVERY_CONCURRENCY", "3")
    monkeypatch.setenv("P20_STAGE_LEASE_SECONDS", "90")
    settings = Settings.from_env()
    assert settings.fetch_concurrency == 4
    assert settings.process_concurrency == 1
    assert settings.delivery_concurrency == 3
    assert settings.stage_lease_seconds == 90