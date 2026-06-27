from __future__ import annotations

from ops_evidence_synthesis.collectors.remote import (
    RemoteCollectorConfig,
    collect_remote_evidence,
    collector_targets_from_more_data,
    write_jsonl_events,
)

__all__ = [
    "RemoteCollectorConfig",
    "collect_remote_evidence",
    "collector_targets_from_more_data",
    "write_jsonl_events",
]
