from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ops_evidence_synthesis.canonical import sha256_text
from ops_evidence_synthesis.synthesis.more_data import filter_more_data_requests
from ops_evidence_synthesis.timeutils import utc_now


DEFAULT_ALLOWED_PATH_ROOTS = (
    "/etc/systemd",
    "/lib/systemd",
    "/usr/lib/systemd",
    "/opt",
    "/srv",
    "/home",
    "/var/lib",
    "/var/log",
    "/run",
)
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.(?:service|timer|path|socket)$")
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_UNIT_IN_TEXT_RE = re.compile(r"\b[A-Za-z0-9_.@:-]+\.(?:service|timer|path|socket)\b")


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandExecutor(Protocol):
    def run(self, argv: list[str], *, timeout_seconds: int) -> CommandResult:
        ...


@dataclass(frozen=True, slots=True)
class RemoteCollectorConfig:
    host: str = "localhost"
    service: str = "ops-evidence"
    environment: str = "prod"
    mode: str = "auto"
    ssh_user: str = ""
    ssh_key_path: str = ""
    timeout_seconds: int = 12
    max_output_chars: int = 12000
    max_journal_lines: int = 80
    max_hash_bytes: int = 10 * 1024 * 1024
    allowed_path_roots: tuple[str, ...] = DEFAULT_ALLOWED_PATH_ROOTS


class SubprocessCommandExecutor:
    def __init__(self, config: RemoteCollectorConfig) -> None:
        self.config = config

    def run(self, argv: list[str], *, timeout_seconds: int) -> CommandResult:
        mode = _resolve_mode(self.config)
        command = argv if mode == "local" else self._ssh_argv(argv)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            return CommandResult(
                argv=tuple(command),
                returncode=int(completed.returncode),
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        except FileNotFoundError as exc:
            return CommandResult(tuple(command), 127, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                tuple(command),
                124,
                exc.stdout or "",
                exc.stderr or f"timed out after {timeout_seconds}s",
                timed_out=True,
            )

    def _ssh_argv(self, argv: list[str]) -> list[str]:
        host = _validated_host(self.config.host)
        if self.config.ssh_user:
            host = f"{self.config.ssh_user}@{host}"
        command = " ".join(shlex.quote(str(item)) for item in argv)
        ssh_argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, min(self.config.timeout_seconds, 30))}",
        ]
        if self.config.ssh_key_path:
            ssh_argv.extend(["-i", self.config.ssh_key_path])
        ssh_argv.extend([host, command])
        return ssh_argv


def collect_remote_evidence(
    config: RemoteCollectorConfig,
    *,
    units: list[str] | tuple[str, ...] | None = None,
    paths: list[str] | tuple[str, ...] | None = None,
    request_ids: list[Any] | tuple[Any, ...] | None = None,
    since: str = "",
    until: str = "",
    executor: CommandExecutor | None = None,
) -> list[dict[str, Any]]:
    """Collect bounded host evidence as JSONL-ready operational events."""

    unit_targets = _unique(_validated_unit(unit) for unit in units or [] if str(unit or "").strip())
    path_targets = _unique(
        _validated_path(path, allowed_roots=config.allowed_path_roots)
        for path in paths or []
        if str(path or "").strip()
    )
    selected = {_safe_request_id(value) for value in request_ids or [] if str(value or "").strip()}
    runner = executor or SubprocessCommandExecutor(config)
    events: list[dict[str, Any]] = []

    for unit in unit_targets:
        if _request_enabled(selected, "job_definition_query", "job_definition"):
            events.append(_event_from_command(config, runner, "systemd_unit_definition", ["systemctl", "cat", unit], unit=unit, request_id="job_definition_query"))
        if _request_enabled(selected, "process_state_query", "process_state"):
            events.append(
                _event_from_command(
                    config,
                    runner,
                    "systemd_unit_state",
                    [
                        "systemctl",
                        "show",
                        unit,
                        "--no-pager",
                        "--property=Id,Names,LoadState,ActiveState,SubState,FragmentPath,UnitFileState,ExecStart,ExecMainStatus,ExecMainPID,Result,ActiveEnterTimestamp,InactiveEnterTimestamp",
                    ],
                    unit=unit,
                    request_id="process_state_query",
                )
            )
            events.append(_event_from_command(config, runner, "systemd_unit_status", ["systemctl", "status", unit, "--no-pager", "--lines", "40"], unit=unit, request_id="process_state_query"))
        if _request_enabled(selected, "scheduler_history_query", "scheduler_history"):
            journal_args = ["journalctl", "-u", unit, "--no-pager", "-o", "short-iso", "-n", str(config.max_journal_lines)]
            if since:
                journal_args.extend(["--since", since])
            if until:
                journal_args.extend(["--until", until])
            events.append(_event_from_command(config, runner, "systemd_journal_sample", journal_args, unit=unit, request_id="scheduler_history_query"))

    for path in path_targets:
        if not _request_enabled(selected, "installed_artifact_query", "installed_artifact"):
            continue
        stat_result = runner.run(["stat", "-c", "%n\t%F\t%s\t%a\t%U\t%G\t%Y", path], timeout_seconds=config.timeout_seconds)
        events.append(_event_from_result(config, "artifact_stat", stat_result, path=path, request_id="installed_artifact_query"))
        if stat_result.returncode == 0 and _stat_size(stat_result.stdout) <= config.max_hash_bytes:
            hash_result = runner.run(["sha256sum", path], timeout_seconds=config.timeout_seconds)
            events.append(_event_from_result(config, "artifact_sha256", hash_result, path=path, request_id="installed_artifact_query"))

    return [event for event in events if event]


def collector_targets_from_more_data(
    query: dict[str, Any] | None,
    *,
    units: list[str] | tuple[str, ...] | None = None,
    paths: list[str] | tuple[str, ...] | None = None,
    request_ids: list[Any] | tuple[Any, ...] | None = None,
) -> dict[str, list[str]]:
    query = query or {}
    selected_requests = filter_more_data_requests(
        [item for item in query.get("next_evidence_requests") or [] if isinstance(item, dict)],
        request_ids,
    )
    selected_ids = {
        _safe_request_id(value)
        for request in selected_requests
        for value in (request.get("request_id"), request.get("profile_request_id"), request.get("request_type"), request.get("need"))
        if value
    }
    if not selected_ids and request_ids:
        selected_ids = {_safe_request_id(value) for value in request_ids if str(value or "").strip()}

    unit_values: list[str] = list(units or [])
    path_values: list[str] = list(paths or [])
    for analysis in query.get("request_analysis") or []:
        if not isinstance(analysis, dict):
            continue
        if selected_ids and _safe_request_id(analysis.get("request_id")) not in selected_ids and _safe_request_id(analysis.get("request_type")) not in selected_ids:
            continue
        request_type = _safe_request_id(analysis.get("request_type"))
        unit_values.extend(str(unit) for unit in analysis.get("units") or [])
        missing_paths = [str(path) for path in analysis.get("missing_paths") or []]
        if request_type == "installed_artifact" and missing_paths:
            path_values.extend(missing_paths)
        else:
            path_values.extend(missing_paths)
            path_values.extend(str(path) for path in analysis.get("paths") or [])
    for row in query.get("preview_rows") or []:
        if not isinstance(row, dict):
            continue
        labels = row.get("labels_json") if isinstance(row.get("labels_json"), dict) else {}
        nested = labels.get("labels") if isinstance(labels.get("labels"), dict) else {}
        unit_values.extend(str(value) for value in (labels.get("systemd_unit"), nested.get("systemd_unit")) if value)
        text = " ".join(str(row.get(key) or "") for key in ("message_sanitized", "message_template", "error_type"))
        unit_values.extend(_UNIT_IN_TEXT_RE.findall(text))

    clean_units: list[str] = []
    for unit in unit_values:
        try:
            clean_units.append(_validated_unit(unit))
        except ValueError:
            continue
    clean_paths: list[str] = []
    for path in path_values:
        try:
            clean_paths.append(_validated_path(path, allowed_roots=DEFAULT_ALLOWED_PATH_ROOTS))
        except ValueError:
            continue
    return {"units": _unique(clean_units), "paths": _unique(clean_paths)}


def write_jsonl_events(events: list[dict[str, Any]], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    return output


def _event_from_command(
    config: RemoteCollectorConfig,
    runner: CommandExecutor,
    kind: str,
    argv: list[str],
    *,
    unit: str = "",
    request_id: str,
) -> dict[str, Any]:
    result = runner.run(argv, timeout_seconds=config.timeout_seconds)
    return _event_from_result(config, kind, result, unit=unit, request_id=request_id)


def _event_from_result(
    config: RemoteCollectorConfig,
    kind: str,
    result: CommandResult,
    *,
    unit: str = "",
    path: str = "",
    request_id: str,
) -> dict[str, Any]:
    ok = result.returncode == 0
    body = _truncate(result.stdout if ok else (result.stderr or result.stdout), config.max_output_chars)
    target = unit or path
    severity = "INFO" if ok else ("WARN" if kind in {"artifact_stat", "artifact_sha256"} else "ERROR")
    message = (
        f"REMOTE_COLLECTOR {kind} host={config.host} target={target} "
        f"returncode={result.returncode} ok={ok}\n{body}"
    ).strip()
    return {
        "timestamp": utc_now(),
        "service": config.service,
        "environment": config.environment,
        "severity": severity,
        "message": message,
        "resource_type": "remote_collector",
        "kind": kind,
        "labels": {
            "component": "remote_collector",
            "collector_host": config.host,
            "collector_mode": _resolve_mode(config),
            "request_id": request_id,
            "unit": unit,
            "path": path,
            "command": _safe_command_label(result.argv),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "stdout_sha256": sha256_text(result.stdout) if result.stdout else "",
            "stderr_sha256": sha256_text(result.stderr) if result.stderr else "",
        },
    }


def _validated_unit(value: Any) -> str:
    unit = str(value or "").strip()
    if not _UNIT_RE.match(unit):
        raise ValueError(f"unsupported systemd unit name: {unit}")
    return unit


def _validated_host(value: Any) -> str:
    host = str(value or "").strip()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return "localhost"
    if not _HOST_RE.match(host):
        raise ValueError(f"unsupported host name: {host}")
    return host


def _validated_path(value: Any, *, allowed_roots: tuple[str, ...]) -> str:
    text = str(value or "").strip().rstrip(":")
    if not text.startswith("/"):
        raise ValueError(f"path must be absolute: {text}")
    path = PurePosixPath(text)
    if ".." in path.parts:
        raise ValueError(f"path cannot contain '..': {text}")
    normalized = path.as_posix()
    if not any(normalized == root.rstrip("/") or normalized.startswith(root.rstrip("/") + "/") for root in allowed_roots):
        raise ValueError(f"path outside allowed roots: {normalized}")
    return normalized


def _resolve_mode(config: RemoteCollectorConfig) -> str:
    mode = config.mode.casefold()
    if mode in {"local", "ssh"}:
        return mode
    host = str(config.host or "").strip()
    return "local" if host in {"", "localhost", "127.0.0.1", "::1"} else "ssh"


def _request_enabled(selected: set[str], *ids: str) -> bool:
    return not selected or bool(selected & {_safe_request_id(value) for value in ids})


def _safe_request_id(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _safe_command_label(argv: tuple[str, ...]) -> str:
    if not argv:
        return ""
    if argv[0] == "ssh":
        return "ssh " + " ".join(argv[-2:])
    return " ".join(argv)


def _stat_size(stdout: str) -> int:
    try:
        return int(str(stdout or "").split("\t")[2])
    except Exception:
        return 0


def _truncate(value: str, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _unique(values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output
