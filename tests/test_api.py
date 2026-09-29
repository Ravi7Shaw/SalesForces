import time

from app.auth import signed_headers
from app.config import settings


def test_health_needs_no_auth(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["auth_enabled"] is True


def test_unauthenticated_request_rejected(client):
    response = client.get("/api/v1/objects")
    assert response.status_code == 401


def test_api_key_auth_accepted(client, auth_headers):
    response = client.get("/api/v1/objects", headers=auth_headers)
    assert response.status_code == 200
    assert len(response.json()) >= 10


def test_wrong_api_key_rejected(client):
    response = client.get("/api/v1/objects", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_hmac_auth_accepted(client):
    headers = signed_headers(settings.hmac_secret, "GET", "/api/v1/objects")
    response = client.get("/api/v1/objects", headers=headers)
    assert response.status_code == 200


def test_hmac_stale_timestamp_rejected(client):
    old_ts = int(time.time()) - settings.auth_max_skew_seconds - 60
    headers = signed_headers(settings.hmac_secret, "GET", "/api/v1/objects", timestamp=old_ts)
    response = client.get("/api/v1/objects", headers=headers)
    assert response.status_code == 401


def test_hmac_replay_rejected(client):
    headers = signed_headers(settings.hmac_secret, "GET", "/api/v1/objects")
    first = client.get("/api/v1/objects", headers=headers)
    second = client.get("/api/v1/objects", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 401


def test_sync_rejects_unknown_object(client, auth_headers):
    response = client.post(
        "/api/v1/jobs/sync",
        headers=auth_headers,
        json={"organization_id": "org1", "objects": ["NotARealObject"]},
    )
    assert response.status_code == 400


def test_sync_deduplicates_objects(client, auth_headers, monkeypatch):
    from app.api.routes import manager

    captured = []
    monkeypatch.setattr(manager, "start", lambda org, objects, records: captured.append(objects) or "synthetic-job")
    response = client.post("/api/v1/jobs/sync", headers=auth_headers,
                           json={"organization_id": "org", "objects": ["Contacts", "Accounts", "Contacts"]})
    assert response.status_code == 202
    assert captured == [["Contacts", "Accounts"]]
