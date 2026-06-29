from __future__ import annotations

import os
import time


POOL_MAX = int(os.environ.get("CHECKOUT_POOL_MAX", "96"))
POOL_WARN_MS = int(os.environ.get("CHECKOUT_POOL_WARN_MS", "250"))
CHECKOUT_TIMEOUT_MS = int(os.environ.get("CHECKOUT_TIMEOUT_MS", "3000"))


class CheckoutPool:
    def __init__(self, max_connections: int) -> None:
        self.max_connections = max_connections
        self.active_connections = 0

    def acquire(self) -> bool:
        if self.active_connections >= self.max_connections:
            log_event(
                "ERROR",
                "database connection pool exhausted",
                metric="db_pool_exhausted_count",
                active=self.active_connections,
                maximum=self.max_connections,
            )
            return False
        self.active_connections += 1
        return True

    def release(self) -> None:
        self.active_connections = max(0, self.active_connections - 1)


def checkout(order_id: str) -> dict[str, str]:
    pool = CheckoutPool(POOL_MAX)
    start = time.monotonic()
    if not pool.acquire():
        log_event("ERROR", "checkout failed HTTP 500 database timeout", metric="checkout_500_count")
        return {"status": "failed", "reason": "db_pool_exhausted"}
    try:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if elapsed_ms > POOL_WARN_MS:
            log_event("WARN", "database connection pool wait exceeded", metric="db_pool_wait_ms")
        return {"status": "ok", "order_id": order_id}
    finally:
        pool.release()


def log_event(level: str, message: str, *, metric: str, **fields: object) -> None:
    field_text = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    print(f"{level} metric={metric} {message} {field_text}".strip())
