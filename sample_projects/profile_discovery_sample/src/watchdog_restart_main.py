from __future__ import annotations

import os


WATCHDOG_METRICS = (
    "job_configuration_mismatch_count",
    "watchdog_heartbeat_count",
)


def main() -> int:
    interval = int(os.environ.get("V3_FAST_RECOVERY_INTERVAL_SEC", "30"))
    print(f"watchdog interval={interval} metric=job_configuration_mismatch_count")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
