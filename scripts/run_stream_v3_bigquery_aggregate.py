from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ops_evidence_synthesis.ai.claude import DEFAULT_CLAUDE_MODEL, VertexClaudeProvider
from ops_evidence_synthesis.ai.maas import (
    DEFAULT_GPT_OSS_MODEL,
    DEFAULT_MISTRAL_MODEL,
    VertexMistralProvider,
    VertexOpenAICompatProvider,
)
from ops_evidence_synthesis.ai.vertex import VertexGeminiProvider
from ops_evidence_synthesis.canonical import sha256_json
from ops_evidence_synthesis.gcp.bigquery import BigQueryOps
from ops_evidence_synthesis.normalize import normalized_event_from_mapping
from ops_evidence_synthesis.profiles import profile_context_for_bundle
from ops_evidence_synthesis.sanitizer import SANITIZER_VERSION
from ops_evidence_synthesis.synthesis.comparison import compare_providers
from ops_evidence_synthesis.synthesis.pipeline import run_synthesis_for_bundle
from ops_evidence_synthesis.synthesis.subsystems import subsystem_for_text
from ops_evidence_synthesis.timeutils import format_timestamp, parse_timestamp, utc_now


DEFAULT_START = "2026-06-02T00:00:00Z"
DEFAULT_END = "2026-06-16T00:00:00Z"


@dataclass(frozen=True, slots=True)
class AnalysisWindows:
    raw_start: str
    raw_end: str
    incident_start: str
    incident_end: str
    lookback_start: str
    baseline_start: str
    baseline_end: str
    lookback_label: str
    baseline_label: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate 14d stream_v3 BigQuery logs and run model synthesis.")
    parser.add_argument("--project", default="ops-evidence-synthesis")
    parser.add_argument("--location", default="asia-northeast1")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--environment", default="stream_v3")
    parser.add_argument("--service", default="stream_v3-aggregate")
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    parser.add_argument("--pattern-limit", type=int, default=80)
    parser.add_argument("--sample-limit", type=int, default=50)
    parser.add_argument("--incident-start", default="")
    parser.add_argument("--incident-end", default="")
    parser.add_argument("--incident-duration-minutes", type=int, default=120)
    parser.add_argument("--lookback-hours", type=int, default=2)
    parser.add_argument("--baseline-days", type=int, default=7)
    parser.add_argument("--include-claude", action="store_true")
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    parser.add_argument("--claude-location", default="global")
    parser.add_argument("--claude-max-output-tokens", type=int, default=8192)
    parser.add_argument("--include-alternative-generators", action="store_true")
    parser.add_argument("--include-gpt-oss", action="store_true")
    parser.add_argument("--gpt-oss-model", default=DEFAULT_GPT_OSS_MODEL)
    parser.add_argument("--gpt-oss-location", default="us-central1")
    parser.add_argument("--gpt-oss-max-output-tokens", type=int, default=4096)
    parser.add_argument("--include-mistral", action="store_true")
    parser.add_argument("--mistral-model", default=DEFAULT_MISTRAL_MODEL)
    parser.add_argument("--mistral-location", default="us-central1")
    parser.add_argument("--mistral-max-output-tokens", type=int, default=8192)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    windows = resolve_windows(args)
    bq = BigQueryOps(args.project, location=args.location)
    bq.apply_schema()
    aggregate_patterns(
        bq,
        environment=args.environment,
        start=windows.lookback_start,
        end=windows.incident_end,
        baseline_start=windows.baseline_start,
        baseline_end=windows.baseline_end,
    )
    aggregate_metrics(
        bq,
        service=args.service,
        environment=args.environment,
        start=windows.incident_start,
        end=windows.incident_end,
        baseline_start=windows.baseline_start,
        baseline_end=windows.baseline_end,
    )
    bundle = build_compact_bundle(
        bq,
        service=args.service,
        environment=args.environment,
        incident_start=windows.incident_start,
        incident_end=windows.incident_end,
        lookback_start=windows.lookback_start,
        baseline_start=windows.baseline_start,
        baseline_end=windows.baseline_end,
        lookback_label=windows.lookback_label,
        baseline_label=windows.baseline_label,
        pattern_limit=args.pattern_limit,
        sample_limit=args.sample_limit,
    )
    bq.delete_synthesis_for_evidence(bundle["evidence_sha256"])
    bq.insert_bundle(bundle)

    providers = [
        VertexGeminiProvider(
            model_name=args.model,
            project_id=args.project,
            location="global",
            max_output_tokens=8192,
        )
    ]
    candidate_providers: list[str] = []
    if args.include_claude:
        provider = VertexClaudeProvider(
            model_name=args.claude_model,
            project_id=args.project,
            location=args.claude_location,
            max_output_tokens=args.claude_max_output_tokens,
        )
        providers.append(provider)
        candidate_providers.append(provider.provider)
    if args.include_alternative_generators or args.include_gpt_oss:
        provider = VertexOpenAICompatProvider(
            model_name=args.gpt_oss_model,
            project_id=args.project,
            location=args.gpt_oss_location,
            max_output_tokens=args.gpt_oss_max_output_tokens,
        )
        providers.append(provider)
        candidate_providers.append(provider.provider)
    if args.include_alternative_generators or args.include_mistral:
        provider = VertexMistralProvider(
            model_name=args.mistral_model,
            project_id=args.project,
            location=args.mistral_location,
            max_output_tokens=args.mistral_max_output_tokens,
        )
        providers.append(provider)
        candidate_providers.append(provider.provider)
    result = run_synthesis_for_bundle(bq, bundle, providers=providers)
    comparisons = []
    for candidate_provider in candidate_providers:
        comparison = compare_providers(
            bq,
            result.evidence_sha256,
            baseline_provider="gemini-enterprise-agent-platform",
            candidate_provider=candidate_provider,
        )
        bq.insert_model_comparison(comparison)
        comparisons.append(comparison)
    proposals = bq.list_proposals(
        limit=20,
        evidence_sha256=result.evidence_sha256,
        pending_only=False,
    )
    print(
        json.dumps(
            {
                "mode": "bigquery-aggregate",
                "raw_log_count": raw_log_count(
                    bq,
                    environment=args.environment,
                    start=windows.raw_start,
                    end=windows.raw_end,
                ),
                "analysis_log_count": raw_log_count(
                    bq,
                    environment=args.environment,
                    start=windows.lookback_start,
                    end=windows.incident_end,
                ),
                "baseline_log_count": raw_log_count(
                    bq,
                    environment=args.environment,
                    start=windows.baseline_start,
                    end=windows.baseline_end,
                ),
                "aggregated_pattern_count": aggregated_pattern_count(
                    bq,
                    environment=args.environment,
                    start=windows.lookback_start,
                    end=windows.incident_end,
                ),
                "incident_window": bundle["incident_window"],
                "lookback": bundle["lookback"],
                "baseline": bundle["baseline"],
                "bundle_log_examples": len(bundle["logs"]),
                "bundle_log_patterns": len(bundle["log_patterns"]),
                "bundle_metric_windows": len(bundle["metric_windows"]),
                "bundle_operational_evidence": len(bundle.get("operational_evidence") or []),
                "bundle_normalized_events": len(bundle.get("normalized_events") or []),
                "evidence_sha256": result.evidence_sha256,
                "providers": [
                    {"provider": provider.provider, "model_name": provider.model_name}
                    for provider in providers
                ],
                "model_run_count": result.model_run_count,
                "parsed_result_count": result.parsed_result_count,
                "claim_count": result.claim_count,
                "proposition_count": result.proposition_count,
                "score_count": result.score_count,
                "cluster_count": result.cluster_count,
                "review_queue_count": result.review_queue_count,
                "comparisons": comparisons,
                "proposals": proposals,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


def resolve_windows(args: argparse.Namespace) -> AnalysisWindows:
    raw_start = format_timestamp(args.start)
    raw_end = format_timestamp(args.end)
    incident_end = format_timestamp(args.incident_end or args.end)
    if args.incident_start:
        incident_start = format_timestamp(args.incident_start)
    else:
        incident_start = format_timestamp(
            parse_timestamp(incident_end) - timedelta(minutes=args.incident_duration_minutes)
        )
    lookback_start = format_timestamp(parse_timestamp(incident_start) - timedelta(hours=args.lookback_hours))
    baseline_start_dt = parse_timestamp(incident_start) - timedelta(days=args.baseline_days)
    baseline_start = format_timestamp(max(parse_timestamp(raw_start), baseline_start_dt))
    baseline_end = incident_start
    return AnalysisWindows(
        raw_start=raw_start,
        raw_end=raw_end,
        incident_start=incident_start,
        incident_end=incident_end,
        lookback_start=lookback_start,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        lookback_label=f"{args.lookback_hours}h",
        baseline_label="previous_7d_same_hour" if args.baseline_days == 7 else f"previous_{args.baseline_days}d_same_hour",
    )


def aggregate_patterns(
    bq: BigQueryOps,
    *,
    environment: str,
    start: str,
    end: str,
    baseline_start: str,
    baseline_end: str,
) -> None:
    params = _window_params(bq, environment=environment, start=start, end=end)
    params.extend(
        [
            bq.bigquery.ScalarQueryParameter("baseline_start", "STRING", baseline_start),
            bq.bigquery.ScalarQueryParameter("baseline_end", "STRING", baseline_end),
        ]
    )
    bq._query(
        f"""
        DELETE FROM `{bq._table("ops_evidence_core", "log_patterns")}`
        WHERE environment = @environment
          AND window_start >= TIMESTAMP(@start)
          AND window_start < TIMESTAMP(@end)
        """,
        params,
    )
    bq._query(
        f"""
        INSERT INTO `{bq._table("ops_evidence_core", "log_patterns")}` (
          pattern_id, service, environment, window_start, window_end, message_template,
          error_type, count, baseline_count, first_seen, last_seen, example_log,
          example_log_sha256, embedding, severity_hint
        )
        WITH hourly AS (
          SELECT
            service,
            environment,
            TIMESTAMP_TRUNC(timestamp, HOUR) AS window_start,
            TIMESTAMP_ADD(TIMESTAMP_TRUNC(timestamp, HOUR), INTERVAL 1 HOUR) AS window_end,
            message_template,
            error_type,
            COUNT(*) AS count,
            MIN(timestamp) AS first_seen,
            MAX(timestamp) AS last_seen,
            ARRAY_AGG(message_sanitized ORDER BY timestamp LIMIT 1)[OFFSET(0)] AS example_log,
            ARRAY_AGG(raw_log_sha256 ORDER BY timestamp LIMIT 1)[OFFSET(0)] AS example_log_sha256,
            MAX(
              CASE severity
                WHEN 'EMERGENCY' THEN 70
                WHEN 'ALERT' THEN 60
                WHEN 'CRITICAL' THEN 50
                WHEN 'ERROR' THEN 40
                WHEN 'WARN' THEN 30
                WHEN 'WARNING' THEN 30
                ELSE 20
              END
            ) AS severity_rank
          FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
          WHERE environment = @environment
            AND timestamp >= TIMESTAMP(@start)
            AND timestamp < TIMESTAMP(@end)
          GROUP BY service, environment, window_start, window_end, message_template, error_type
        ),
        baseline AS (
          SELECT
            service,
            message_template,
            error_type,
            COUNT(*) AS baseline_total,
            COUNT(DISTINCT TIMESTAMP_TRUNC(timestamp, HOUR)) AS baseline_hour_count
          FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
          WHERE environment = @environment
            AND timestamp >= TIMESTAMP(@baseline_start)
            AND timestamp < TIMESTAMP(@baseline_end)
          GROUP BY service, message_template, error_type
        )
        SELECT
          CONCAT(
            'PATTERN-',
            SUBSTR(TO_HEX(SHA256(CONCAT(
              h.service, '|', h.environment, '|', CAST(h.window_start AS STRING), '|',
              h.message_template, '|', h.error_type
            ))), 1, 16)
          ) AS pattern_id,
          h.service,
          h.environment,
          h.window_start,
          h.window_end,
          h.message_template,
          h.error_type,
          h.count,
          CAST(ROUND(COALESCE(SAFE_DIVIDE(b.baseline_total, NULLIF(b.baseline_hour_count, 0)), 0)) AS INT64)
            AS baseline_count,
          h.first_seen,
          h.last_seen,
          h.example_log,
          h.example_log_sha256,
          ARRAY<FLOAT64>[] AS embedding,
          CASE
            WHEN h.severity_rank >= 50 THEN 'critical'
            WHEN h.severity_rank >= 40 THEN 'high'
            WHEN h.severity_rank >= 30 THEN 'medium'
            WHEN h.count >= 100 THEN 'medium'
            ELSE 'low'
          END AS severity_hint
        FROM hourly h
        LEFT JOIN baseline b
          ON b.service = h.service
         AND b.message_template = h.message_template
         AND b.error_type = h.error_type
        """,
        params,
    )


def aggregate_metrics(
    bq: BigQueryOps,
    *,
    service: str,
    environment: str,
    start: str,
    end: str,
    baseline_start: str,
    baseline_end: str,
) -> None:
    params = _window_params(bq, environment=environment, start=start, end=end)
    params.extend(
        [
            bq.bigquery.ScalarQueryParameter("service", "STRING", service),
            bq.bigquery.ScalarQueryParameter("baseline_start", "STRING", baseline_start),
            bq.bigquery.ScalarQueryParameter("baseline_end", "STRING", baseline_end),
        ]
    )
    bq._query(
        f"""
        DELETE FROM `{bq._table("ops_evidence_core", "metric_windows")}`
        WHERE service = @service
          AND window_start = TIMESTAMP(@start)
          AND window_end = TIMESTAMP(@end)
        """,
        params,
    )
    bq._query(
        f"""
        INSERT INTO `{bq._table("ops_evidence_core", "metric_windows")}` (
          metric_window_id, service, window_start, window_end, metric_name,
          baseline_value, current_value, delta, delta_pct, severity_hint
        )
        WITH current_hours AS (
          SELECT DISTINCT EXTRACT(HOUR FROM timestamp) AS hour
          FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
          WHERE environment = @environment
            AND timestamp >= TIMESTAMP(@start)
            AND timestamp < TIMESTAMP(@end)
        ),
        base AS (
          SELECT
            COUNT(*) AS total_log_count,
            COUNTIF(severity IN ('WARN', 'WARNING')) AS warn_count,
            COUNTIF(severity IN ('ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY')) AS error_count,
            COUNTIF(error_type = 'stream_transport') AS stream_transport_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%active stream%' OR LOWER(message_sanitized) LIKE '%stream active%') AS active_stream_count,
            COUNTIF(error_type = 'youtube_health') AS youtube_health_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%youtube%' AND (LOWER(message_sanitized) LIKE '%ingest%' OR LOWER(message_sanitized) LIKE '%watch url%' OR LOWER(message_sanitized) LIKE '%watchdog%')) AS youtube_ingest_count,
            COUNTIF(error_type = 'service_health_failure') AS service_health_failure_count,
            COUNTIF(error_type = 'runtime_restart') AS runtime_restart_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%connection reset%') AS connection_reset_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%rtmps%' AND (LOWER(message_sanitized) LIKE '%reconnect%' OR LOWER(message_sanitized) LIKE '%connect%' OR LOWER(message_sanitized) LIKE '%send-path%')) AS rtmps_reconnect_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%ffmpeg%' AND (LOWER(message_sanitized) LIKE '%exit%' OR LOWER(message_sanitized) LIKE '%rc=%' OR LOWER(message_sanitized) LIKE '%process%' OR LOWER(message_sanitized) LIKE '%track finished%')) AS ffmpeg_process_state_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%audio_energy%' OR LOWER(message_sanitized) LIKE '%audio energy%' OR LOWER(message_sanitized) LIKE '%silence%') AS audio_energy_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%capture_freshness%' OR LOWER(message_sanitized) LIKE '%capture freshness%' OR LOWER(message_sanitized) LIKE '%chromium%' OR LOWER(message_sanitized) LIKE '%renderer%' OR LOWER(message_sanitized) LIKE '%crashpad%') AS capture_freshness_count,
            COUNT(DISTINCT service) AS active_service_count,
            COUNT(DISTINCT TIMESTAMP_TRUNC(timestamp, HOUR)) AS active_hour_count
          FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
          WHERE environment = @environment
            AND timestamp >= TIMESTAMP(@start)
            AND timestamp < TIMESTAMP(@end)
        ),
        baseline_base AS (
          SELECT
            COUNT(*) AS total_log_count,
            COUNTIF(severity IN ('WARN', 'WARNING')) AS warn_count,
            COUNTIF(severity IN ('ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY')) AS error_count,
            COUNTIF(error_type = 'stream_transport') AS stream_transport_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%active stream%' OR LOWER(message_sanitized) LIKE '%stream active%') AS active_stream_count,
            COUNTIF(error_type = 'youtube_health') AS youtube_health_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%youtube%' AND (LOWER(message_sanitized) LIKE '%ingest%' OR LOWER(message_sanitized) LIKE '%watch url%' OR LOWER(message_sanitized) LIKE '%watchdog%')) AS youtube_ingest_count,
            COUNTIF(error_type = 'service_health_failure') AS service_health_failure_count,
            COUNTIF(error_type = 'runtime_restart') AS runtime_restart_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%connection reset%') AS connection_reset_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%rtmps%' AND (LOWER(message_sanitized) LIKE '%reconnect%' OR LOWER(message_sanitized) LIKE '%connect%' OR LOWER(message_sanitized) LIKE '%send-path%')) AS rtmps_reconnect_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%ffmpeg%' AND (LOWER(message_sanitized) LIKE '%exit%' OR LOWER(message_sanitized) LIKE '%rc=%' OR LOWER(message_sanitized) LIKE '%process%' OR LOWER(message_sanitized) LIKE '%track finished%')) AS ffmpeg_process_state_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%audio_energy%' OR LOWER(message_sanitized) LIKE '%audio energy%' OR LOWER(message_sanitized) LIKE '%silence%') AS audio_energy_count,
            COUNTIF(LOWER(message_sanitized) LIKE '%capture_freshness%' OR LOWER(message_sanitized) LIKE '%capture freshness%' OR LOWER(message_sanitized) LIKE '%chromium%' OR LOWER(message_sanitized) LIKE '%renderer%' OR LOWER(message_sanitized) LIKE '%crashpad%') AS capture_freshness_count,
            COUNT(DISTINCT service) AS active_service_count,
            COUNT(DISTINCT TIMESTAMP_TRUNC(timestamp, HOUR)) AS active_hour_count
          FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
          WHERE environment = @environment
            AND timestamp >= TIMESTAMP(@baseline_start)
            AND timestamp < TIMESTAMP(@baseline_end)
            AND EXTRACT(HOUR FROM timestamp) IN (SELECT hour FROM current_hours)
        ),
        baseline_days AS (
          SELECT GREATEST(DATE_DIFF(DATE(TIMESTAMP(@baseline_end)), DATE(TIMESTAMP(@baseline_start)), DAY), 1) AS days
        ),
        metrics AS (
          SELECT 'total_log_count' AS metric_name, total_log_count AS value FROM base UNION ALL
          SELECT 'warn_count', warn_count FROM base UNION ALL
          SELECT 'error_count', error_count FROM base UNION ALL
          SELECT 'stream_transport_count', stream_transport_count FROM base UNION ALL
          SELECT 'active_stream_count', active_stream_count FROM base UNION ALL
          SELECT 'youtube_health_count', youtube_health_count FROM base UNION ALL
          SELECT 'youtube_ingest_count', youtube_ingest_count FROM base UNION ALL
          SELECT 'service_health_failure_count', service_health_failure_count FROM base UNION ALL
          SELECT 'runtime_restart_count', runtime_restart_count FROM base UNION ALL
          SELECT 'connection_reset_count', connection_reset_count FROM base UNION ALL
          SELECT 'rtmps_reconnect_count', rtmps_reconnect_count FROM base UNION ALL
          SELECT 'ffmpeg_process_state_count', ffmpeg_process_state_count FROM base UNION ALL
          SELECT 'audio_energy_count', audio_energy_count FROM base UNION ALL
          SELECT 'capture_freshness_count', capture_freshness_count FROM base UNION ALL
          SELECT 'active_service_count', active_service_count FROM base UNION ALL
          SELECT 'active_hour_count', active_hour_count FROM base
        ),
        baseline_metrics AS (
          SELECT 'total_log_count' AS metric_name, total_log_count AS value FROM baseline_base UNION ALL
          SELECT 'warn_count', warn_count FROM baseline_base UNION ALL
          SELECT 'error_count', error_count FROM baseline_base UNION ALL
          SELECT 'stream_transport_count', stream_transport_count FROM baseline_base UNION ALL
          SELECT 'active_stream_count', active_stream_count FROM baseline_base UNION ALL
          SELECT 'youtube_health_count', youtube_health_count FROM baseline_base UNION ALL
          SELECT 'youtube_ingest_count', youtube_ingest_count FROM baseline_base UNION ALL
          SELECT 'service_health_failure_count', service_health_failure_count FROM baseline_base UNION ALL
          SELECT 'runtime_restart_count', runtime_restart_count FROM baseline_base UNION ALL
          SELECT 'connection_reset_count', connection_reset_count FROM baseline_base UNION ALL
          SELECT 'rtmps_reconnect_count', rtmps_reconnect_count FROM baseline_base UNION ALL
          SELECT 'ffmpeg_process_state_count', ffmpeg_process_state_count FROM baseline_base UNION ALL
          SELECT 'audio_energy_count', audio_energy_count FROM baseline_base UNION ALL
          SELECT 'capture_freshness_count', capture_freshness_count FROM baseline_base UNION ALL
          SELECT 'active_service_count', active_service_count FROM baseline_base UNION ALL
          SELECT 'active_hour_count', active_hour_count FROM baseline_base
        )
        SELECT
          CONCAT('METRIC-', LPAD(CAST(ROW_NUMBER() OVER (ORDER BY m.metric_name) AS STRING), 3, '0')) AS metric_window_id,
          @service AS service,
          TIMESTAMP(@start) AS window_start,
          TIMESTAMP(@end) AS window_end,
          m.metric_name,
          SAFE_DIVIDE(CAST(COALESCE(b.value, 0) AS FLOAT64), CAST(d.days AS FLOAT64)) AS baseline_value,
          CAST(m.value AS FLOAT64) AS current_value,
          CAST(m.value AS FLOAT64) - SAFE_DIVIDE(CAST(COALESCE(b.value, 0) AS FLOAT64), CAST(d.days AS FLOAT64)) AS delta,
          SAFE_DIVIDE(
            CAST(m.value AS FLOAT64) - SAFE_DIVIDE(CAST(COALESCE(b.value, 0) AS FLOAT64), CAST(d.days AS FLOAT64)),
            NULLIF(SAFE_DIVIDE(CAST(COALESCE(b.value, 0) AS FLOAT64), CAST(d.days AS FLOAT64)), 0)
          ) AS delta_pct,
          CASE
            WHEN m.metric_name IN ('stream_transport_count', 'active_stream_count', 'audio_energy_count', 'capture_freshness_count')
             AND m.value = 0
             AND SAFE_DIVIDE(CAST(COALESCE(b.value, 0) AS FLOAT64), CAST(d.days AS FLOAT64)) > 0 THEN 'high'
            WHEN m.metric_name IN ('error_count', 'stream_transport_count', 'service_health_failure_count', 'connection_reset_count')
             AND m.value > SAFE_DIVIDE(CAST(COALESCE(b.value, 0) AS FLOAT64), CAST(d.days AS FLOAT64)) THEN 'high'
            WHEN m.value > 0 THEN 'medium'
            ELSE 'low'
          END AS severity_hint
        FROM metrics m
        LEFT JOIN baseline_metrics b USING(metric_name)
        CROSS JOIN baseline_days d
        """,
        params,
    )


def build_compact_bundle(
    bq: BigQueryOps,
    *,
    service: str,
    environment: str,
    incident_start: str,
    incident_end: str,
    lookback_start: str,
    baseline_start: str,
    baseline_end: str,
    lookback_label: str,
    baseline_label: str,
    pattern_limit: int,
    sample_limit: int,
) -> dict[str, Any]:
    params = _window_params(bq, environment=environment, start=lookback_start, end=incident_end)
    params.extend(
        [
            bq.bigquery.ScalarQueryParameter("pattern_limit", "INT64", pattern_limit),
            bq.bigquery.ScalarQueryParameter("sample_limit", "INT64", sample_limit),
        ]
    )
    patterns = [
        _compact_pattern(_stringify_timestamps(dict(row)))
        for row in bq._query(
            f"""
            SELECT *
            FROM `{bq._table("ops_evidence_core", "log_patterns")}`
            WHERE environment = @environment
              AND window_start >= TIMESTAMP(@start)
              AND window_start < TIMESTAMP(@end)
              AND (
                error_type != 'none'
                OR severity_hint IN ('critical', 'high')
                OR LOWER(message_template) LIKE '%rtmps%'
                OR LOWER(message_template) LIKE '%connection reset%'
                OR LOWER(message_template) LIKE '%youtube%'
              )
            ORDER BY
              CASE severity_hint
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'medium' THEN 2
                ELSE 3
              END,
              CASE WHEN error_type = 'none' THEN 1 ELSE 0 END,
              count DESC,
              first_seen DESC,
              pattern_id
            LIMIT @pattern_limit
            """,
            params,
        )
    ]
    logs = [
        _compact_log(_stringify_timestamps(dict(row)))
        for row in bq._query(
            f"""
            SELECT * EXCEPT(rn)
            FROM (
              SELECT
                raw_log_sha256 AS log_id,
                timestamp,
                service,
                environment,
                severity,
                trace_id,
                span_id,
                deploy_id,
                version,
                message_sanitized,
                message_template,
                error_type,
                stack_hash,
                resource_type,
                labels_json,
                raw_log_sha256,
                sanitizer_version,
                ROW_NUMBER() OVER (
                  PARTITION BY service, error_type, message_template
                  ORDER BY
                    CASE severity
                      WHEN 'EMERGENCY' THEN 70
                      WHEN 'ALERT' THEN 60
                      WHEN 'CRITICAL' THEN 50
                      WHEN 'ERROR' THEN 40
                      WHEN 'WARN' THEN 30
                      WHEN 'WARNING' THEN 30
                      ELSE 20
                    END DESC,
                    timestamp DESC,
                    raw_log_sha256
                ) AS rn
              FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
              WHERE environment = @environment
                AND timestamp >= TIMESTAMP(@start)
                AND timestamp < TIMESTAMP(@end)
                AND (
                  error_type != 'none'
                  OR severity IN ('WARN', 'WARNING', 'ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY')
                  OR LOWER(message_sanitized) LIKE '%connection reset%'
                  OR LOWER(message_sanitized) LIKE '%rtmps%'
                )
            )
            WHERE rn = 1
            ORDER BY timestamp DESC, raw_log_sha256
            LIMIT @sample_limit
            """,
            params,
        )
    ]
    metrics = [
        _stringify_timestamps(dict(row))
        for row in bq._query(
            f"""
            SELECT *
            FROM `{bq._table("ops_evidence_core", "metric_windows")}`
            WHERE service = @service
              AND window_start = TIMESTAMP(@incident_start)
              AND window_end = TIMESTAMP(@incident_end)
            ORDER BY metric_window_id
            """,
            [
                bq.bigquery.ScalarQueryParameter("environment", "STRING", environment),
                bq.bigquery.ScalarQueryParameter("service", "STRING", service),
                bq.bigquery.ScalarQueryParameter("incident_start", "STRING", incident_start),
                bq.bigquery.ScalarQueryParameter("incident_end", "STRING", incident_end),
            ],
        )
    ]
    operational_evidence = build_operational_evidence(
        bq,
        environment=environment,
        incident_start=incident_start,
        incident_end=incident_end,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        sample_limit=min(sample_limit, 8),
    )
    for index, log in enumerate(logs, start=1):
        log["evidence_id"] = f"LOG-{index:03d}"
        log["labels_json"] = _compact_labels(log.get("labels_json"))
    evidence_refs: dict[str, Any] = {}
    for log in logs:
        evidence_refs[log["evidence_id"]] = {
            "type": "representative_log",
            "summary": log["message_sanitized"],
            "timestamp": log["timestamp"],
            "subsystem": log["subsystem"],
        }
    for pattern in patterns:
        evidence_refs[pattern["pattern_id"]] = {
            "type": "hourly_log_pattern",
            "summary": pattern["message_template"],
            "count": pattern["count"],
            "baseline_count": pattern.get("baseline_count"),
            "first_seen": pattern["first_seen"],
            "last_seen": pattern["last_seen"],
            "subsystem": pattern["subsystem"],
        }
    for metric in metrics:
        subsystem = subsystem_for_text(metric["metric_name"])
        evidence_refs[metric["metric_window_id"]] = {
            "type": "metric_window",
            "summary": f"{metric['metric_name']}={metric['current_value']}",
            "current_value": metric.get("current_value"),
            "baseline_value": metric.get("baseline_value"),
            "delta": metric.get("delta"),
            "delta_pct": metric.get("delta_pct"),
            "subsystem": subsystem,
        }
    normalized_events = [
        normalized_event_from_mapping(log, source_system=environment, environment=environment)
        for log in logs[:50]
    ]
    for item in operational_evidence:
        for sample in item.get("samples") or []:
            event = normalized_event_from_mapping(
                {**sample, "subsystem": item.get("subsystem")},
                source_system=environment,
                environment=environment,
            )
            event["component"] = str(item.get("subsystem") or event.get("component") or "unknown")
            normalized_events.append(event)
    for item in operational_evidence:
        evidence_refs[item["evidence_id"]] = {
            "type": "operational_evidence",
            "summary": item["summary"],
            "incident_count": item["incident_count"],
            "baseline_count": item["baseline_count"],
            "baseline_daily_average": item["baseline_daily_average"],
            "subsystem": item["subsystem"],
            "request_id": item["request_id"],
            "profile_request_id": item.get("profile_request_id", ""),
            "sample_count": len(item["samples"]),
        }

    query_fingerprint = {
        "service": service,
        "environment": environment,
        "incident_start": incident_start,
        "incident_end": incident_end,
        "lookback_start": lookback_start,
        "baseline_start": baseline_start,
        "baseline_end": baseline_end,
        "builder": "bigquery-hourly-aggregate-v3",
        "pattern_limit": pattern_limit,
        "sample_limit": sample_limit,
    }
    profile_context = profile_context_for_bundle({"environment": environment, "profile": {"profile_id": "stream_v3"}})
    bundle = {
        "schema_version": "ops-evidence-bundle/v1",
        **profile_context,
        "service": service,
        "environment": environment,
        "window_start": incident_start,
        "window_end": incident_end,
        "incident_window": {
            "start": incident_start,
            "end": incident_end,
        },
        "lookback": lookback_label,
        "baseline": {
            "mode": baseline_label,
            "start": baseline_start,
            "end": baseline_end,
        },
        "lookback_window_start": lookback_start,
        "lookback_minutes": int((parse_timestamp(incident_start) - parse_timestamp(lookback_start)).total_seconds() // 60),
        "query_sql_hash": sha256_json(query_fingerprint),
        "sanitizer_version": SANITIZER_VERSION,
        "incident": {
            "incident_id": f"INC-{sha256_json(query_fingerprint)[:12]}",
            "duration_minutes": int((parse_timestamp(incident_end) - parse_timestamp(incident_start)).total_seconds() // 60),
        },
        "evidence_refs": evidence_refs,
        "normalized_events": normalized_events[:100],
        "logs": logs,
        "log_patterns": patterns,
        "metric_windows": metrics,
        "operational_evidence": operational_evidence,
        "deployments": [],
        "similar_past_incidents": [],
        "created_at": utc_now(),
    }
    hash_payload = {key: value for key, value in bundle.items() if key not in {"created_at", "evidence_sha256"}}
    bundle["evidence_sha256"] = sha256_json(hash_payload)
    return bundle


def build_operational_evidence(
    bq: BigQueryOps,
    *,
    environment: str,
    incident_start: str,
    incident_end: str,
    baseline_start: str,
    baseline_end: str,
    sample_limit: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    params = [
        bq.bigquery.ScalarQueryParameter("environment", "STRING", environment),
        bq.bigquery.ScalarQueryParameter("incident_start", "STRING", incident_start),
        bq.bigquery.ScalarQueryParameter("incident_end", "STRING", incident_end),
        bq.bigquery.ScalarQueryParameter("baseline_start", "STRING", baseline_start),
        bq.bigquery.ScalarQueryParameter("baseline_end", "STRING", baseline_end),
    ]
    limit = max(1, min(int(sample_limit), 20))
    for index, spec in enumerate(_operational_evidence_specs(), start=1):
        rows = list(
            bq._query(
                f"""
                WITH baseline_days AS (
                  SELECT GREATEST(DATE_DIFF(DATE(TIMESTAMP(@baseline_end)), DATE(TIMESTAMP(@baseline_start)), DAY), 1) AS days
                ),
                incident_rows AS (
                  SELECT timestamp, service, severity, message_sanitized, message_template, error_type, labels_json, raw_log_sha256
                  FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
                  WHERE environment = @environment
                    AND timestamp >= TIMESTAMP(@incident_start)
                    AND timestamp < TIMESTAMP(@incident_end)
                    AND ({spec["predicate"]})
                ),
                baseline_rows AS (
                  SELECT raw_log_sha256
                  FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
                  WHERE environment = @environment
                    AND timestamp >= TIMESTAMP(@baseline_start)
                    AND timestamp < TIMESTAMP(@baseline_end)
                    AND ({spec["predicate"]})
                )
                SELECT
                  (SELECT COUNT(*) FROM incident_rows) AS incident_count,
                  (SELECT COUNT(*) FROM baseline_rows) AS baseline_count,
                  SAFE_DIVIDE((SELECT COUNT(*) FROM baseline_rows), (SELECT days FROM baseline_days)) AS baseline_daily_average,
                  ARRAY(
                    SELECT AS STRUCT timestamp, service, severity, message_sanitized, message_template, error_type, labels_json, raw_log_sha256
                    FROM incident_rows
                    ORDER BY timestamp DESC
                    LIMIT {limit}
                  ) AS samples
                """,
                params,
            )
        )
        row = dict(rows[0]) if rows else {}
        samples = [
            _compact_operational_sample(_stringify_timestamps(dict(sample)))
            for sample in row.get("samples") or []
        ]
        incident_count = int(row.get("incident_count") or 0)
        baseline_count = int(row.get("baseline_count") or 0)
        baseline_daily_average = float(row.get("baseline_daily_average") or 0.0)
        output.append(
            {
                "evidence_id": f"OPS-{index:03d}",
                "request_id": spec["request_id"],
                "profile_request_id": spec.get("profile_request_id", ""),
                "need": spec["need"],
                "summary": spec["summary"],
                "subsystem": spec["subsystem"],
                "incident_count": incident_count,
                "baseline_count": baseline_count,
                "baseline_daily_average": round(baseline_daily_average, 4),
                "samples": samples,
                "interpretation": _operational_interpretation(
                    spec,
                    incident_count=incident_count,
                    baseline_daily_average=baseline_daily_average,
                ),
            }
        )
    return output


def _operational_evidence_specs() -> list[dict[str, str]]:
    text = "LOWER(CONCAT(COALESCE(message_sanitized, ''), ' ', COALESCE(message_template, ''), ' ', COALESCE(error_type, ''), ' ', COALESCE(TO_JSON_STRING(labels_json), '')))"
    return [
        {
            "request_id": "process_state_query",
            "profile_request_id": "ffmpeg_state_query",
            "need": "process_state",
            "subsystem": "rtmps_ffmpeg",
            "summary": "ffmpeg process state and exit code evidence.",
            "predicate": f"{text} LIKE '%ffmpeg%' AND ({text} LIKE '%exit%' OR {text} LIKE '%rc=%' OR {text} LIKE '%process%' OR {text} LIKE '%track finished%' OR {text} LIKE '%systemd%')",
        },
        {
            "request_id": "throughput_signal_query",
            "profile_request_id": "rtmps_reconnect_query",
            "need": "throughput_signal",
            "subsystem": "rtmps_ffmpeg",
            "summary": "RTMPS connection, reconnect, and send-path evidence.",
            "predicate": f"({text} LIKE '%rtmps%' OR {text} LIKE '%stream_transport%' OR {text} LIKE '%stream transport%') AND ({text} LIKE '%reconnect%' OR {text} LIKE '%connect%' OR {text} LIKE '%send-path%' OR {text} LIKE '%tcp send%')",
        },
        {
            "request_id": "external_dependency_status_query",
            "profile_request_id": "youtube_ingest_status_query",
            "need": "external_dependency_status",
            "subsystem": "youtube_health",
            "summary": "YouTube ingest, watchdog, and watch URL evidence.",
            "predicate": f"{text} LIKE '%youtube%' OR {text} LIKE '%watchdog%' OR {text} LIKE '%watch url%' OR {text} LIKE '%youtube_health%'",
        },
        {
            "request_id": "user_impact_signal_query",
            "profile_request_id": "audio_energy_gap_query",
            "need": "user_impact_signal",
            "subsystem": "audio_energy",
            "summary": "Audio energy, silence, and PulseAudio evidence.",
            "predicate": f"{text} LIKE '%audio_energy%' OR {text} LIKE '%audio energy%' OR {text} LIKE '%silence%' OR {text} LIKE '%pulseaudio%'",
        },
        {
            "request_id": "freshness_signal_query",
            "profile_request_id": "capture_freshness_query",
            "need": "freshness_signal",
            "subsystem": "chromium_capture",
            "summary": "Capture freshness, Chromium renderer, and crashpad evidence.",
            "predicate": f"{text} LIKE '%capture_freshness%' OR {text} LIKE '%capture freshness%' OR {text} LIKE '%chromium%' OR {text} LIKE '%renderer%' OR {text} LIKE '%crashpad%'",
        },
        {
            "request_id": "network_path_query",
            "profile_request_id": "network_reset_by_destination_query",
            "need": "network_path",
            "subsystem": "network_transport",
            "summary": "Connection reset, TCP, and packet-loss evidence.",
            "predicate": f"{text} LIKE '%connection reset%' OR {text} LIKE '%notsent%' OR {text} LIKE '%unacked%' OR {text} LIKE '%packet%' OR {text} LIKE '%retransmit%'",
        },
        {
            "request_id": "state_transition_query",
            "profile_request_id": "stream_service_substate_query",
            "need": "state_transition",
            "subsystem": "runtime_recovery",
            "summary": "stream_service_substate, watchdog_ok, and service state evidence.",
            "predicate": f"{text} LIKE '%stream_service_substate%' OR {text} LIKE '%watchdog_ok%' OR {text} LIKE '%substate%' OR {text} LIKE '%systemd%' OR {text} LIKE '%service state%'",
        },
    ]


def _operational_interpretation(spec: dict[str, str], *, incident_count: int, baseline_daily_average: float) -> str:
    if incident_count == 0 and baseline_daily_average > 0:
        return "zero_is_bad_or_evidence_gap: this source was present in baseline but absent in the incident window."
    if incident_count > 0 and baseline_daily_average == 0:
        return "new_incident_evidence: this source appeared in the incident window but not in baseline."
    if incident_count > 0:
        return "evidence_available: inspect samples before accepting or rejecting the review target."
    return "no_evidence_available: use this as a missing-evidence signal, not proof of normal behavior."


def _compact_operational_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": str(row.get("timestamp") or ""),
        "service": str(row.get("service") or ""),
        "severity": str(row.get("severity") or ""),
        "message_sanitized": _compact_text(row.get("message_sanitized"), limit=360),
        "message_template": _compact_text(row.get("message_template"), limit=220),
        "error_type": str(row.get("error_type") or ""),
        "labels_json": _compact_labels(row.get("labels_json")),
        "raw_log_sha256": str(row.get("raw_log_sha256") or ""),
    }


def raw_log_count(bq: BigQueryOps, *, environment: str, start: str, end: str) -> int:
    rows = bq._query(
        f"""
        SELECT COUNT(*) AS count
        FROM `{bq._table("ops_evidence_raw", "logs_sanitized")}`
        WHERE environment = @environment
          AND timestamp >= TIMESTAMP(@start)
          AND timestamp < TIMESTAMP(@end)
        """,
        _window_params(bq, environment=environment, start=start, end=end),
    )
    return int(list(rows)[0]["count"])


def aggregated_pattern_count(bq: BigQueryOps, *, environment: str, start: str, end: str) -> int:
    rows = bq._query(
        f"""
        SELECT COUNT(*) AS count
        FROM `{bq._table("ops_evidence_core", "log_patterns")}`
        WHERE environment = @environment
          AND window_start >= TIMESTAMP(@start)
          AND window_start < TIMESTAMP(@end)
        """,
        _window_params(bq, environment=environment, start=start, end=end),
    )
    return int(list(rows)[0]["count"])


def _window_params(bq: BigQueryOps, *, environment: str, start: str, end: str) -> list[Any]:
    return [
        bq.bigquery.ScalarQueryParameter("environment", "STRING", environment),
        bq.bigquery.ScalarQueryParameter("start", "STRING", start),
        bq.bigquery.ScalarQueryParameter("end", "STRING", end),
    ]


def _stringify_timestamps(row: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(row.items()):
        if hasattr(value, "isoformat"):
            row[key] = value.isoformat().replace("+00:00", "Z")
    return row


def _compact_labels(labels: Any) -> dict[str, Any]:
    if not isinstance(labels, dict):
        return {}
    compact = {}
    for key in ("source_path", "source_line", "kind", "status", "failure_kind", "action", "query"):
        if key in labels:
            compact[key] = _safe_source_path(labels[key]) if key == "source_path" else labels[key]
    return compact


def _safe_source_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    filename = text.replace("\\", "/").rstrip("/").split("/")[-1] or "source"
    return f"sanitized://{filename}#{sha256_json({'source_path': text})[:16]}"


def _compact_pattern(row: dict[str, Any]) -> dict[str, Any]:
    row["message_template"] = _compact_text(row.get("message_template"), limit=300)
    row["example_log"] = _compact_text(row.get("example_log"), limit=500)
    row["embedding"] = []
    row["subsystem"] = subsystem_for_text(f"{row.get('error_type')} {row.get('message_template')} {row.get('example_log')}")
    return row


def _compact_log(row: dict[str, Any]) -> dict[str, Any]:
    row["message_sanitized"] = _compact_text(row.get("message_sanitized"), limit=500)
    row["message_template"] = _compact_text(row.get("message_template"), limit=300)
    row["subsystem"] = subsystem_for_text(f"{row.get('error_type')} {row.get('message_template')} {row.get('message_sanitized')}")
    return row


def _compact_text(value: Any, *, limit: int) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated {len(text) - limit} chars]"


if __name__ == "__main__":
    raise SystemExit(main())
