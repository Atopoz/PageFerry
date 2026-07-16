import sqlite3

from fastapi.testclient import TestClient

from core.settings import Settings
from main import create_app


def test_startup_creates_local_data_layout_and_database(tmp_path) -> None:
    app = create_app(Settings(data_dir=tmp_path))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "code": "success",
        "data": {"service": "pageferry-api", "version": "0.1.0"},
    }
    for directory in ("workspace", "outputs", "models", "cache", "logs"):
        assert (tmp_path / directory).is_dir()

    with sqlite3.connect(tmp_path / "pageferry.sqlite3") as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'translation_jobs'"
        ).fetchone()
    assert table == ("translation_jobs",)


def test_model_catalog_is_versioned_and_contains_bootstrap_providers(tmp_path) -> None:
    app = create_app(Settings(data_dir=tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/v1/model-catalog")

    assert response.status_code == 200
    catalog = response.json()
    assert catalog["schema_version"] == 1
    assert catalog["catalog_version"] == "0.1.0-dev"
    assert {provider["id"] for provider in catalog["providers"]} >= {
        "openai",
        "gemini",
        "custom_openai",
    }
