from __future__ import annotations

from collections import defaultdict
from typing import Any

from ops_evidence_synthesis.models import PropositionClusterRecord
from ops_evidence_synthesis.profiles import profile_id_for_item, profile_label, title_for_target_type
from ops_evidence_synthesis.timeutils import utc_now


def persist_proposition_clusters(
    store: Any,
    evidence_sha256: str,
    *,
    limit: int = 1000,
) -> tuple[PropositionClusterRecord, ...]:
    if not hasattr(store, "insert_proposition_clusters"):
        return ()
    proposals = store.list_proposals(
        limit=limit,
        evidence_sha256=evidence_sha256,
        pending_only=False,
        include_hidden=True,
    )
    clusters = build_proposition_clusters(proposals)
    store.insert_proposition_clusters(clusters)
    return clusters


def build_proposition_clusters(proposals: list[dict[str, Any]]) -> tuple[PropositionClusterRecord, ...]:
    now = utc_now()
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        cluster_id = str(proposal.get("cluster_id") or "")
        if not cluster_id:
            continue
        by_cluster[cluster_id].append(proposal)

    clusters: list[PropositionClusterRecord] = []
    primary_cluster_id, primary_title = _primary_incident_cluster(by_cluster)
    for cluster_id, members in sorted(by_cluster.items()):
        representative = _representative(members)
        parent_cluster_id = ""
        relationship = ""
        if primary_cluster_id and cluster_id != primary_cluster_id and _is_validation_child_cluster(representative):
            parent_cluster_id = primary_cluster_id
            relationship = "validation_target_for_primary_incident"
        all_member_ids = _unique(
            str(member_id)
            for item in members
            for member_id in (item.get("cluster_member_ids") or [item.get("proposition_id")])
            if member_id
        )
        providers = _unique(
            str(provider)
            for item in members
            for provider in (item.get("model_providers") or _split_csv(item.get("model_provider")))
            if provider
        )
        model_names = _unique(
            str(model_name)
            for item in members
            for model_name in (item.get("model_names") or _split_csv(item.get("model_name")))
            if model_name
        )
        core_claim = _core_claim(representative)
        clusters.append(
            PropositionClusterRecord(
                cluster_id=cluster_id,
                evidence_sha256=str(representative.get("evidence_sha256") or ""),
                subsystem=str(representative.get("subsystem") or "general"),
                claim_signature=str(representative.get("cluster_signature") or ""),
                representative_proposition_id=str(representative.get("proposition_id") or ""),
                member_proposition_ids=tuple(all_member_ids),
                supporting_providers=tuple(providers),
                model_names=tuple(model_names),
                core_claim=core_claim,
                disagreement_summary=_disagreement_summary(members, providers),
                review_status=str(representative.get("review_status") or "pending"),
                review_visibility=str(representative.get("review_visibility") or "review"),
                review_priority_score=round(float(representative.get("review_priority_score") or 0.0), 4),
                cluster_json={
                    "cluster_id": cluster_id,
                    "parent_cluster_id": parent_cluster_id,
                    "parent_title": primary_title if parent_cluster_id else "",
                    "relationship": relationship,
                    "core_target_type": str(representative.get("cluster_signature") or ""),
                    "profile": {
                        "profile_id": profile_id_for_item(representative),
                        "profile_label": profile_label(profile_id_for_item(representative)),
                    },
                    "cluster_size": len(all_member_ids),
                    "representative_proposition_id": str(representative.get("proposition_id") or ""),
                    "member_proposition_ids": all_member_ids,
                    "supporting_providers": providers,
                    "model_names": model_names,
                    "core_claim": core_claim,
                    "next_data_needed": list(representative.get("next_data_needed") or []),
                    "members": [_cluster_member_json(item) for item in members],
                },
                created_at=now,
            )
        )
    return tuple(clusters)


def _primary_incident_cluster(by_cluster: dict[str, list[dict[str, Any]]]) -> tuple[str, str]:
    candidates: list[tuple[float, str, str]] = []
    for cluster_id, members in by_cluster.items():
        representative = _representative(members)
        signature = str(representative.get("cluster_signature") or "")
        review_mode = str(representative.get("review_mode") or "")
        title = _title_for_cluster(representative)
        if review_mode != "incident_candidate" and signature != "throughput_disappearance":
            continue
        candidates.append((float(representative.get("review_priority_score") or 0.0), cluster_id, title))
    if not candidates:
        return "", ""
    _, cluster_id, title = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
    return cluster_id, title


def _is_validation_child_cluster(item: dict[str, Any]) -> bool:
    if str(item.get("review_mode") or "") == "validation_target":
        return True
    return str(item.get("cluster_signature") or "") in {
        "external_dependency_failure",
        "network_error_signal",
        "user_impact_signal_gap",
        "freshness_signal_gap",
        "state_mismatch",
        "monitoring_gap",
        "instrumentation_mismatch",
        "observability_contract_mismatch",
    }


def _title_for_cluster(item: dict[str, Any]) -> str:
    signature = str(item.get("cluster_signature") or "")
    profile_id = profile_id_for_item(item)
    if signature:
        return title_for_target_type(signature, profile_id, default=signature.replace("_", " ").title())
    for key in ("support_summary", "counter_summary", "question"):
        value = str(item.get(key) or "").strip()
        if value:
            return value[:160]
    return str(item.get("cluster_id") or "")


def _representative(members: list[dict[str, Any]]) -> dict[str, Any]:
    representatives = [item for item in members if item.get("cluster_representative")]
    candidates = representatives or members
    return sorted(
        candidates,
        key=lambda item: (
            -float(item.get("review_priority_score") or 0.0),
            str(item.get("proposition_id") or ""),
        ),
    )[0]


def _core_claim(item: dict[str, Any]) -> str:
    for key in ("support_summary", "counter_summary", "question"):
        value = str(item.get(key) or "").strip()
        if value:
            return value[:1200]
    return ""


def _disagreement_summary(members: list[dict[str, Any]], providers: list[str]) -> str:
    hidden = [item for item in members if item.get("review_visibility") == "hidden"]
    monitor = [item for item in members if item.get("review_visibility") == "monitor_only"]
    if len(providers) > 1:
        return (
            f"{len(providers)} providers map to this cluster. "
            f"{len(hidden)} duplicate/hidden members and {len(monitor)} monitor-only members were found."
        )
    if hidden:
        return f"{len(hidden)} duplicate/hidden members were found within one provider family."
    if monitor:
        return f"{len(monitor)} members are monitor-only because the evidence is stable or improved."
    return "No cross-model disagreement recorded for this cluster."


def _cluster_member_json(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposition_id": item.get("proposition_id"),
        "question": item.get("question"),
        "subsystem": item.get("subsystem"),
        "review_visibility": item.get("review_visibility"),
        "hidden_reason": item.get("hidden_reason"),
        "review_priority_score": item.get("review_priority_score"),
        "model_provider": item.get("model_provider"),
        "model_name": item.get("model_name"),
        "support_summary": item.get("support_summary"),
        "counter_summary": item.get("counter_summary"),
        "next_data_needed": item.get("next_data_needed") or [],
    }


def _split_csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _unique(values: Any) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        text = str(value).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)
