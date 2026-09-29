from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.services import retry


@pytest.mark.parametrize("value", ["120", "date"])
def test_retry_after_is_not_capped(value, monkeypatch):
    monkeypatch.setattr(retry.time, "time", lambda: 1000)
    if value == "date":
        value = format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True)
    calls, sleeps = [], []

    def request():
        calls.append(True)
        if len(calls) == 1:
            httpx.Response(429, headers={"Retry-After": value}, request=httpx.Request("GET", "http://mock")).raise_for_status()
        return "ok"

    assert retry.retry_call(request, sleep=sleeps.append) == "ok"
    assert sleeps == [120]
    assert len(calls) == 2
