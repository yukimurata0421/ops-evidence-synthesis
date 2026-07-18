from __future__ import annotations

from ops_evidence_synthesis.profiles import (
    available_profile_ids,
    evidence_requests_for_target_type,
    load_profile,
    metric_names_by_zero_behavior,
    metric_semantics,
    operational_evidence_specs,
    profile_context_for_bundle,
    profile_for_bundle,
    target_definition,
    target_type_for_subsystem,
    title_for_target_type,
)


def test_stream_v3_is_profile_mapping_not_core_target_type() -> None:
    profile = profile_for_bundle({"environment": "stream_v3"})
    semantics = metric_semantics("stream_transport_count", "stream_v3")
    requests = evidence_requests_for_target_type("throughput_disappearance", "stream_v3")

    assert profile["profile_id"] == "stream_v3"
    assert semantics["semantic_type"] == "throughput"
    assert semantics["zero_behavior"] == "suspicious"
    assert semantics["core_target_type"] == "throughput_disappearance"
    assert title_for_target_type("throughput_disappearance", "stream_v3") == "Stream transport disappeared"
    assert requests[0]["request_id"] == "process_state_query"
    assert requests[0]["profile_request_id"] == "ffmpeg_state_query"


def test_generic_profile_has_abstract_evidence_requests() -> None:
    requests = evidence_requests_for_target_type("throughput_disappearance", "generic")

    assert requests[0]["request_id"] == "process_state_query"
    assert "profile_request_id" not in requests[0]
    assert requests[0]["request_type"] == "process_state"


def test_profile_maps_subsystems_to_core_target_types() -> None:
    assert target_type_for_subsystem("chromium_capture", "stream_v3") == "freshness_signal_gap"
    assert target_type_for_subsystem("rtmps_ffmpeg", "stream_v3") == "throughput_disappearance"
    assert target_type_for_subsystem("traffic", "generic") == "throughput_disappearance"


def test_profile_context_is_loaded_for_arbitrary_named_system() -> None:
    profile = profile_for_bundle({"environment": "amazon-notify"})
    service_profile = profile_for_bundle({"environment": "prod", "service": "amazon-notify", "profile": {"profile_id": "generic"}})
    context = profile_context_for_bundle({"environment": "amazon-notify"})
    requests = evidence_requests_for_target_type("job_configuration_mismatch", "amazon_notify")

    assert profile["profile_id"] == "amazon_notify"
    assert service_profile["profile_id"] == "amazon_notify"
    assert context["system_profile"]["system_type"] == "notification_workflow"
    assert context["review_policy"]["context_is_not_evidence"] is True
    assert requests[0]["request_id"] == "job_definition_query"
    assert requests[1]["request_id"] == "installed_artifact_query"


def test_local_profile_semantic_rules_require_explicit_trust(monkeypatch, tmp_path) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "local_unapproved.json").write_text(
        '{"profile_id":"local_unapproved","event_semantics":[{"id":"rule-1","match":{"component":"worker"},"event_name":"restart_loop"}]}',
        encoding="utf-8",
    )
    (profile_dir / "local_approved.json").write_text(
        '{"profile_id":"local_approved","semantic_rule_trust":"human_approved","event_semantics":[{"id":"rule-1","match":{"component":"worker"},"event_name":"restart_loop"}]}',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    load_profile.cache_clear()

    unapproved = profile_context_for_bundle({"profile_id": "local_unapproved"})
    approved = profile_context_for_bundle({"profile_id": "local_approved"})

    assert unapproved["semantic_rule_trust"] == "unapproved"
    assert approved["semantic_rule_trust"] == "human_approved"


def test_stream_v3_monitoring_profile_maps_arena_server_service() -> None:
    profile = profile_for_bundle({"service": "stream_v3_monitoring", "environment": "arena_server"})
    context = profile_context_for_bundle({"service": "stream_v3_monitoring", "environment": "arena_server"})
    requests = evidence_requests_for_target_type("throughput_disappearance", "stream_v3_monitoring")

    assert profile["profile_id"] == "stream_v3_monitoring"
    assert context["runtime_ownership"]["primary_runtime"] == "kubernetes:stream-v3/stream-v3-runtime"
    assert context["classification_overrides"][0]["id"] == "arena_local_stream_dead_under_k8s_runtime"
    assert requests[0]["request_id"] == "rtmps_send_path_query"
    assert requests[0]["profile_request_id"] == "rtmps_send_path_query"


def test_operational_evidence_specs_are_profile_driven() -> None:
    specs = operational_evidence_specs("stream_v3_monitoring")

    assert "stream_v3_monitoring" in available_profile_ids()
    assert specs[0]["evidence_id"] == "OPS-001"
    assert specs[0]["request_id"] == "ffmpeg_process_state_query"
    assert specs[0]["subsystem"] == "rtmps_ffmpeg"
    assert "stream_v3_runtime_ffmpeg_present" in specs[0]["metric_names"]


def test_zero_semantics_collects_metrics_from_all_profiles() -> None:
    suspicious = metric_names_by_zero_behavior("suspicious")

    assert "stream_transport_count" in suspicious
    assert "runtime_ffmpeg_present" in suspicious


def test_target_review_priority_policy_can_live_in_profile() -> None:
    generic = target_definition("throughput_disappearance", "generic")
    stream = target_definition("throughput_disappearance", "stream_v3_monitoring")
    instrumentation = target_definition("instrumentation_mismatch", "stream_v3")

    assert generic["primary_incident"] is True
    assert generic["zero_bad_incident_score_floor"] == 0.90
    assert stream["validation_score_cap"] == 0.84
    assert instrumentation["validation_score_cap"] == 0.62
    assert evidence_requests_for_target_type("instrumentation_mismatch", "stream_v3")[0]["request_id"] == "instrumentation_consistency_query"


def test_profile_loader_accepts_standard_yaml(tmp_path, monkeypatch) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "yaml_system.yaml").write_text(
        "\n".join(
            [
                "profile_id: yaml_system",
                "profile_label: YAML System",
                "source_system: yaml-demo",
                "system_profile:",
                "  system_type: batch_job",
                "metrics:",
                "  job_success_count:",
                "    zero_behavior: suspicious",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OES_PROFILE_DIR", str(profile_dir))
    load_profile.cache_clear()

    profile = load_profile("yaml-system")

    assert profile["profile_id"] == "yaml_system"
    assert profile["system_profile"]["system_type"] == "batch_job"
    assert profile["metrics"]["job_success_count"]["zero_behavior"] == "suspicious"
    load_profile.cache_clear()
