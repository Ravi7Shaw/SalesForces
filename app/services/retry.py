import random
import time
from collections.abc import Callable

import httpx


def retry_call(fn: Callable, attempts: int = 5, base_delay: float = 0.5):
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt == attempts - 1:
                raise
            delay = min(30.0, base_delay * (2**attempt) + random.random() * 0.2)
            if isinstance(exc, httpx.HTTPStatusError):
                retryable = exc.response.status_code in {408, 429, 500, 502, 503, 504}
                if not retryable:
                    raise
                retry_after = exc.response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(30.0, max(delay, float(retry_after)))
                    except ValueError:
                        pass
            time.sleep(delay)
    raise last
