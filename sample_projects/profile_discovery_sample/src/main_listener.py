from __future__ import annotations

import os


def run_listener() -> None:
    label = os.environ.get("MESSAGE_LABEL", "inbox")
    print(f"main listener active label={label} metric=watchdog_heartbeat_count")
