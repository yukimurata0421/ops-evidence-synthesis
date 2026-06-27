from __future__ import annotations

from pathlib import Path

from ops_evidence_synthesis.collectors.remote import (
    CommandResult,
    RemoteCollectorConfig,
    collect_remote_evidence,
    collector_targets_from_more_data,
    write_jsonl_events,
)
from ops_evidence_synthesis.ingest import ingest_log_files
from ops_evidence_synthesis.storage.sqlite_store import SQLiteStore


class FakeExecutor:
    def run(self, argv: list[str], *, timeout_seconds: int) -> CommandResult:
        del timeout_seconds
        command = " ".join(argv)
        if argv[:2] == ["systemctl", "cat"]:
            return CommandResult(tuple(argv), 0, "[Service]\nExecStart=/usr/bin/python3 /home/example/app/job.py\n", "")
        if argv[:2] == ["systemctl", "show"]:
            return CommandResult(tuple(argv), 0, "Id=amazon-notify.service\nActiveState=failed\nSubState=failed\n", "")
        if argv[:2] == ["systemctl", "status"]:
            return CommandResult(tuple(argv), 3, "amazon-notify.service failed with result exit-code", "")
        if argv[:2] == ["journalctl", "-u"]:
            return CommandResult(tuple(argv), 0, "2026-06-16T00:00:00Z amazon-notify.service failed\n", "")
        if argv[:2] == ["stat", "-c"]:
            return CommandResult(tuple(argv), 0, "/home/example/app/job.py\tregular file\t12\t755\tapp\tapp\t1780000000\n", "")
        if argv[0] == "sha256sum":
            return CommandResult(tuple(argv), 0, "abc123  /home/example/app/job.py\n", "")
        raise AssertionError(f"unexpected command: {command}")


def test_remote_collector_collects_systemd_and_artifact_events(tmp_path: Path) -> None:
    config = RemoteCollectorConfig(
        host="localhost",
        service="amazon-notify",
        environment="prod",
        mode="local",
    )

    events = collect_remote_evidence(
        config,
        units=["amazon-notify.service"],
        paths=["/home/example/app/job.py"],
        executor=FakeExecutor(),
    )
    kinds = [event["kind"] for event in events]

    assert kinds == [
        "systemd_unit_definition",
        "systemd_unit_state",
        "systemd_unit_status",
        "systemd_journal_sample",
        "artifact_stat",
        "artifact_sha256",
    ]
    assert events[0]["labels"]["request_id"] == "job_definition_query"
    assert "ExecStart=/usr/bin/python3 /home/example/app/job.py" in events[0]["message"]
    assert events[4]["labels"]["path"] == "/home/example/app/job.py"

    output = write_jsonl_events(events, tmp_path / "collector.jsonl")
    store = SQLiteStore(tmp_path / "oes.sqlite3")
    assert ingest_log_files([output], store) == len(events)


def test_collector_targets_are_inferred_from_more_data_analysis() -> None:
    query = {
        "next_evidence_requests": [
            {"request_id": "job_definition_query", "request_type": "job_definition"},
            {"request_id": "installed_artifact_query", "request_type": "installed_artifact"},
        ],
        "request_analysis": [
            {
                "request_id": "job_definition_query",
                "request_type": "job_definition",
                "units": ["amazon-notify.service"],
                "paths": ["/home/example/app/job.py"],
            },
            {
                "request_id": "installed_artifact_query",
                "request_type": "installed_artifact",
                "missing_paths": ["/home/example/app/job.py"],
                "paths": ["/home/example/app/.venv/bin/python", "/home/example/app/job.py"],
            },
        ],
    }

    targets = collector_targets_from_more_data(query)
    installed_only = collector_targets_from_more_data(query, request_ids=["installed_artifact_query"])

    assert targets == {"units": ["amazon-notify.service"], "paths": ["/home/example/app/job.py"]}
    assert installed_only == {"units": [], "paths": ["/home/example/app/job.py"]}
