import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.services.salesforce import SalesforceBulkClient
from app.services.schema import OBJECTS, csv_rows
from mock_salesforce.main import app as mock_app


@pytest.mark.parametrize("base_url", ["http://localhost:9003", "http://mock-salesforce:9000"])
def test_oauth_and_all_objects_against_actual_mock(monkeypatch, base_url):
    monkeypatch.setattr(settings, "salesforce_client_id", "mock-client")
    monkeypatch.setattr(settings, "salesforce_refresh_token", "mock-refresh")
    monkeypatch.setattr(settings, "salesforce_login_url", "")
    with TestClient(mock_app, base_url=base_url) as http:
        client = SalesforceBulkClient(client=http, base_url=base_url)
        for obj in OBJECTS:
            count = 4501 if obj == "Accounts" else 3
            job_id, text = client.run(obj, count)
            assert job_id
            assert client.base == base_url
            assert len(csv_rows(obj, text, "org")) == count


def test_mock_requires_form_fields():
    with TestClient(mock_app) as client:
        response = client.post("/services/oauth2/token", params={
            "grant_type": "refresh_token", "client_id": "mock", "refresh_token": "mock"})
        assert response.status_code == 400


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_poll_timeout_includes_http_time(monkeypatch):
    clock = Clock()
    requests = []
    monkeypatch.setattr(settings, "job_timeout_seconds", 1)

    def slow_status(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] <= 1
        clock.now += 2
        return httpx.Response(200, json={"state": "JobComplete"})

    with httpx.Client(transport=httpx.MockTransport(slow_status)) as http:
        client = SalesforceBulkClient(client=http, base_url="http://mock", clock=clock, sleep=clock.sleep)
        client._token = "synthetic"
        with pytest.raises(TimeoutError):
            client.wait_for_job("job")
    assert len(requests) == 1
    assert clock.sleeps == []


def test_poll_deadline_includes_token_refresh(monkeypatch):
    clock = Clock()
    paths = []
    monkeypatch.setattr(settings, "job_timeout_seconds", 1)
    monkeypatch.setattr(settings, "salesforce_login_url", "")

    def response(request):
        paths.append(request.url.path)
        clock.now += 0.6
        if request.url.path.endswith("/token"):
            assert request.extensions["timeout"]["read"] == pytest.approx(0.4)
            return httpx.Response(200, json={"access_token": "replacement"})
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        client = SalesforceBulkClient(client=http, base_url="http://mock", clock=clock, sleep=clock.sleep)
        client._token = "expired"
        with pytest.raises(TimeoutError):
            client.wait_for_job("job")
    assert len(paths) == 2
    assert paths[-1].endswith("/token")


def test_poll_sleep_stops_at_deadline(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(settings, "job_timeout_seconds", 1)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"state": "InProgress"}))) as http:
        client = SalesforceBulkClient(client=http, base_url="http://mock", clock=clock, sleep=clock.sleep)
        client._token = "synthetic"
        with pytest.raises(TimeoutError):
            client.wait_for_job("job")
    assert clock.now == pytest.approx(1)


def test_poll_retry_after_cannot_overrun_deadline(monkeypatch):
    clock = Clock()
    calls = []
    monkeypatch.setattr(settings, "job_timeout_seconds", 10)

    def response(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        client = SalesforceBulkClient(client=http, base_url="http://mock", clock=clock, sleep=clock.sleep)
        client._token = "synthetic"
        with pytest.raises(TimeoutError):
            client.wait_for_job("job")
    assert len(calls) == 1
    assert clock.sleeps == []
