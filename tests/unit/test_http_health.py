from fastapi.testclient import TestClient

from screener.infrastructure.http.app import create_app


def test_healthz() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_metrics() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "instruments_active" in resp.json()
