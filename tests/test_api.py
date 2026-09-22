from fastapi.testclient import TestClient

from app.main import app


def test_health():
    assert TestClient(app).get("/health").status_code == 200


def test_objects():
    response = TestClient(app).get("/api/v1/objects")
    assert response.status_code == 200
    assert len(response.json()) >= 10
