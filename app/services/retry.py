import random
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime

import httpx
from botocore import exceptions as boto_exc
from clickhouse_connect.driver.exceptions import OperationalError as ClickHouseOperationalError

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def is_retryable(exc: BaseException) -> bool:
    """Rate limits, 5xx and network-level failures are retried; everything else is a bug or a hard error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, boto_exc.ClientError):
        meta = exc.response.get("ResponseMetadata", {})
        code = exc.response.get("Error", {}).get("Code", "")
        return meta.get("HTTPStatusCode", 0) >= 500 or code in {"SlowDown", "Throttling", "RequestTimeout"}
    if isinstance(exc, (boto_exc.ConnectionError, boto_exc.HTTPClientError)):
        return True
    if isinstance(exc, ClickHouseOperationalError):
        return True
    return False


def retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    value = exc.response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:  # HTTP-date form
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def retry_call(
    fn: Callable,
    attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
):
    """Call ``fn`` with exponential backoff + jitter; honours Retry-After on 408/429/5xx."""
    for attempt in range(attempts):
        if deadline is not None and clock() >= deadline:
            raise TimeoutError("Operation deadline exceeded")
        try:
            result = fn()
            if deadline is not None and clock() >= deadline:
                raise TimeoutError("Operation deadline exceeded")
            return result
        except Exception as exc:
            if deadline is not None and clock() >= deadline:
                raise TimeoutError("Operation deadline exceeded") from exc
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2**attempt) + random.random() * 0.2)
            retry_after = retry_after_seconds(exc)
            if retry_after is not None:
                delay = max(delay, retry_after)
            if deadline is not None and delay >= deadline - clock():
                # Do not retry earlier than requested just to fit the deadline.
                raise TimeoutError("Retry delay exceeds operation deadline") from exc
            sleep(delay)
