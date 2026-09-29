"""Freeze the currently observed forecast graph for prospective audits, without reprocessing.

This is not a reconstruction of an earlier date. Parsed text, stored vectors and predictions
are copied; original binary files, provider model weights and exact LLM requests are not.
A signal cap fails closed before replacing an existing snapshot. One read-only repeatable-read
transaction captures the graph; serialization writes one JSONL record at a time atomically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import now_utc
from app.db.models import (
    Document,
    DocumentChunk,
    EvalRun,
    InstitutionRow,
    LLMCall,
    Opportunity,
    OpportunityRelation,
    OpportunitySignal,
    ReviewItem,
    Signal,
    Source,
)
from app.domain.stages import CANCELS_KEY, COMMITMENT_MULTIPLIER, STAGE_PRIOR
from app.eval.review_dataset import _changes, _history
from app.llm.prompts import EXTRACT_PROMPT_VERSION
from app.llm.schemas import EXTRACTION_SCHEMA_VERSION
from app.pipeline.backtest import METHOD_VERSION
from app.pipeline.link import WEIGHTS
from app.pipeline.recommend import RANKER_VERSION
from app.settings import Settings, get_settings

SNAPSHOT_VERSION = "forecast-snapshot-v1"
_SETTING_FIELDS = (
    "llm_provider",
    "llm_extract_model",
    "llm_extract_effort",
    "embedding_provider",
    "embedding_dim",
    "voyage_model",
    "triage_threshold",
    "link_threshold",
    "link_review_band",
    "grounding_min_score",
    "ocr_provider",
    "ocr_languages",
    "ocr_min_text_chars_per_page",
)
_STRUCTURED_FIELDS = (
    "meeting_date",
    "published_from",
    "fiscal_year",
    "budget_kind",
    "order_year",
    "order_month",
    "amount_krw",
    "notice_kind",
    "order_plan_no",
    "prespec_no",
    "bid_notice_no",
    "bid_notice_nos",
    "council",
    "council_id",
    "term",
    "session",
    "sitting",
    "meeting",
    "docid",
    "revision_docids",
    "pages",
    "ocr_pages",
)
_SIGNAL_FIELDS = (
    "id",
    "document_id",
    "chunk_id",
    "stage",
    "institution_code",
    "speaker_institution_code",
    "department",
    "title",
    "summary",
    "category",
    "keywords",
    "budget_krw",
    "expected_year",
    "expected_half",
    "commitment",
    "procurement_type",
    "confidence",
    "verdict",
    "extractor",
    "observed_at",
    "created_at",
    "dedupe_key",
)
_OPPORTUNITY_FIELDS = (
    "id",
    "institution_code",
    "department",
    "title",
    "category",
    "stage",
    "status",
    "first_seen_at",
    "last_signal_at",
    "bid_window_start",
    "bid_window_end",
    "bid_published_at",
    "est_budget_krw",
    "best_commitment",
    "signal_count",
    "conversion_prob",
    "keywords",
    "created_at",
    "updated_at",
)
_REF_FIELDS = ("order_plan_no", "prespec_no", "bid_notice_no", "bid_notice_nos", CANCELS_KEY)
_EVIDENCE_FIELDS = ("quote", "source_quote", "found", "score", "start", "end", "method")
_GROUNDING_FIELDS = (
    "budget_claimed",
    "budget_parsed",
    "budget_grounded",
    "year_claimed",
    "year_resolved",
    "year_grounded",
    "evidence_ratio",
    "verdict",
    "source",
)


class SnapshotLimitError(ValueError):
    """The requested graph exceeds the cap; no partial snapshot was published."""


@dataclass(slots=True)
class ForecastGraph:
    documents: list[Document] = field(default_factory=list)
    chunks: list[DocumentChunk] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)
    links: list[OpportunitySignal] = field(default_factory=list)
    reviews: list[ReviewItem] = field(default_factory=list)
    relations: list[OpportunityRelation] = field(default_factory=list)
    institutions: list[InstitutionRow] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    calibration: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ForecastSnapshotReport:
    version: str
    captured_at: str
    institution_code: str
    code_revision: str
    digest: str
    counts: dict[str, int]
    incomplete_reasons: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ForecastSnapshot:
    records: list[dict[str, Any]]
    report: ForecastSnapshotReport

    def iter_jsonl_bytes(self) -> Iterator[bytes]:
        yield _line(
            self.report.to_json()
            | {
                "record_type": "forecast_snapshot_manifest",
                "digest_scope": "following_record_lines",
                "code_revision_provenance": "operator_attested_not_git_verified",
                "temporal_scope": "current_observed_state_including_late_ingestion",
                "historical_as_of_verified": False,
                "captured_at_basis": "application_clock_before_consistent_snapshot_queries",
                "captured_at_is_exact_database_as_of": False,
                "database_isolation": "repeatable_read_read_only",
                "parsing_replay_possible": False,
                "raw_binary_not_embedded": True,
                "exact_llm_call_replay_possible": False,
                "model_weights_embedded": False,
                "prediction_origin": "stored_not_recomputed",
                "model_configuration_origin": "capture_process_not_verified_original_worker",
                "scope_policy": "institution_owner_signals_and_opportunities_plus_membership_closure",
                "privacy_policy": "allowlisted_public_source_state_no_tenant_profiles_or_user_names",
            }
        )
        for record in self.records:
            yield _line(record)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Snapshot datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_value(value.tolist())
    return value


def _line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _pick(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: _json_value(getattr(row, name)) for name in fields}


def _safe_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    # These named source/configuration fields are scalars or lists, never arbitrary JSON.
    return {
        name: _json_value(value[name])
        for name in fields
        if name in value
        and (
            value[name] is None
            or isinstance(value[name], (str, int, float, bool))
            or (
                isinstance(value[name], list)
                and all(isinstance(v, (str, int, float, bool)) for v in value[name])
            )
        )
    }


def _evidence(value: Any) -> list[dict[str, Any]]:
    return (
        [_safe_fields(item, _EVIDENCE_FIELDS) for item in value] if isinstance(value, list) else []
    )


def _grounding(value: dict[str, Any]) -> dict[str, Any]:
    safe = _safe_fields(value, _GROUNDING_FIELDS)
    safe["evidence"] = _evidence(value.get("evidence", []))
    # Error payloads after ':' and freeform notes can contain provider/request details.
    safe["issues"] = [
        issue.split(":", 1)[0]
        for issue in value.get("issues", [])
        if isinstance(issue, str) and re.fullmatch(r"[a-z_]+(?::.*)?", issue)
    ]
    audit = value.get("revalidation")
    if isinstance(audit, dict):
        safe["revalidation"] = _safe_fields(audit, ("version", "digest", "at", "previous_verdict"))
        if isinstance(audit.get("report"), dict):
            safe["revalidation"]["report"] = _grounding(audit["report"])
    return safe


def _vector(value: Any, missing: Counter[str], reason: str) -> dict[str, Any]:
    if value is None:
        missing[reason] += 1
        return {"present": False, "values": None, "dimension": None}
    values = [float(item) for item in value]
    if not values or not all(math.isfinite(item) for item in values):
        raise ValueError("Stored embedding contains invalid values")
    return {"present": True, "values": values, "dimension": len(values)}


def _validate_options(institution_code: str, code_revision: str, max_signals: int) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", institution_code):
        raise ValueError("institution_code must be a registry code")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", code_revision):
        raise ValueError(
            "code_revision must be an operator-attested hexadecimal revision (7..64 characters)"
        )
    if max_signals < 1:
        raise SnapshotLimitError("max_signals must be positive; no snapshot written")


def assemble_forecast_snapshot(
    graph: ForecastGraph,
    *,
    institution_code: str,
    code_revision: str,
    captured_at: datetime,
    settings: Settings,
) -> ForecastSnapshot:
    """Serialize a graph already read from one snapshot; caller supplies its real capture clock."""
    _validate_options(institution_code, code_revision, max_signals=max(1, len(graph.signals)))
    if captured_at.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    records: list[dict[str, Any]] = []
    missing: Counter[str] = Counter()

    def add(entity: str, data: dict[str, Any], *, prediction: bool = False) -> None:
        records.append(
            {
                "record_type": "prediction" if prediction else "frozen_input",
                "entity": entity,
                "data": _json_value(data),
            }
        )

    add(
        "configuration",
        {
            "captured_at": captured_at,
            "code_revision": code_revision.lower(),
            "code_revision_provenance": "operator_attested_not_git_verified",
            "settings": _pick(settings, _SETTING_FIELDS),
            "extract_prompt_version": EXTRACT_PROMPT_VERSION,
            "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
            "ranker_version": RANKER_VERSION,
            "backtest_method_version": METHOD_VERSION,
            "link_weights": WEIGHTS,
            "stage_priors": {key.value: value for key, value in STAGE_PRIOR.items()},
            "commitment_multipliers": COMMITMENT_MULTIPLIER,
            "current_calibration": graph.calibration,
            "original_forecast_calibration_recorded": False,
        },
    )
    for row in sorted(graph.institutions, key=lambda row: row.code):
        add(
            "institution",
            _pick(
                row,
                (
                    "code",
                    "name",
                    "kind",
                    "sido",
                    "sigungu",
                    "region_code",
                    "executive_code",
                    "aliases",
                ),
            ),
        )
    for source_row in sorted(graph.sources, key=lambda row: row.id):
        add(
            "source",
            _pick(
                source_row,
                ("id", "key", "name", "adapter", "enabled", "created_at", "last_success_at"),
            ),
        )
    document_ids = {row.id for row in graph.documents}
    signal_ids = {row.id for row in graph.signals}
    opportunity_ids = {row.id for row in graph.opportunities}
    for document in sorted(graph.documents, key=lambda row: row.id):
        if not document.text:
            missing["document_text_missing"] += 1
        if document.parse_status != "parsed":
            missing["document_not_parsed"] += 1
        if not re.fullmatch(r"[0-9a-fA-F]{64}", document.content_hash or ""):
            missing["document_content_hash_invalid"] += 1
        data = _pick(
            document,
            (
                "id",
                "source_id",
                "doc_type",
                "title",
                "publisher_raw",
                "institution_code",
                "department",
                "published_at",
                "content_hash",
                "mime",
                "text",
                "text_quality",
                "parse_method",
                "parse_status",
                "created_at",
                "updated_at",
                "extracted_at",
            ),
        )
        data["structured"] = _safe_fields(document.structured, _STRUCTURED_FIELDS)
        data["text_sha256"] = (
            hashlib.sha256(document.text.encode()).hexdigest()
            if document.text is not None
            else None
        )
        data["publication_basis"] = data["structured"].get(
            "published_from", "stored_date_unspecified"
        )
        data["raw_blob_reference_present"] = bool(document.raw_uri)
        data["raw_binary_embedded"] = False
        add("document", data)
    for chunk in sorted(graph.chunks, key=lambda row: (row.document_id, row.seq, row.id)):
        add(
            "chunk",
            _pick(
                chunk,
                (
                    "id",
                    "document_id",
                    "seq",
                    "char_start",
                    "char_end",
                    "text",
                    "labels",
                    "triage_score",
                    "triage_passed",
                ),
            ),
        )
    for signal in sorted(graph.signals, key=lambda row: row.id):
        data = _pick(signal, _SIGNAL_FIELDS)
        data["embedding"] = _vector(signal.embedding, missing, "signal_embedding_missing")
        data["evidence"] = _evidence(signal.evidence)
        data["grounding"] = _grounding(signal.grounding or {})
        data["external_refs"] = _safe_fields(signal.external_refs, _REF_FIELDS)
        if signal.document_id not in document_ids:
            missing["signal_document_missing"] += 1
        if not signal.evidence:
            missing["signal_evidence_missing"] += 1
        if not signal.extractor:
            missing["signal_extractor_missing"] += 1
        if signal.institution_code != institution_code:
            missing["cross_institution_membership_signal"] += 1
        add("signal", data)
    for link in sorted(graph.links, key=lambda row: (row.opportunity_id, row.signal_id)):
        data = _pick(
            link, ("opportunity_id", "signal_id", "score", "method", "tentative", "created_at")
        )
        data["reasons"] = _safe_fields(
            link.reasons, ("semantic", "title", "category", "budget", "timeline", "exclusive")
        )
        if isinstance(link.reasons.get("ref"), dict):
            data["reasons"]["ref"] = _safe_fields(link.reasons["ref"], _REF_FIELDS)
        if link.signal_id not in signal_ids or link.opportunity_id not in opportunity_ids:
            missing["membership_endpoint_missing"] += 1
        add("link", data)
    for review in sorted(graph.reviews, key=lambda row: row.id):
        data = _pick(
            review, ("id", "signal_id", "status", "resolved_by", "resolved_at", "created_at")
        )
        data["resolution"] = {
            "action": review.resolution.get("action")
            if review.resolution.get("action") in ("approve", "edit", "reject")
            else None,
            "changes": _changes(review.resolution),
            "history": _history(review.resolution),
        }
        data["gold_standard"] = False
        add("review", data)
    for relation in sorted(graph.relations, key=lambda row: row.id):
        data = _pick(
            relation,
            (
                "id",
                "kind",
                "project_id",
                "contract_id",
                "status",
                "version",
                "evidence_signal_ids",
                "updated_by",
                "updated_at",
                "created_at",
            ),
        )
        data["evidence_snapshot"] = [
            _safe_fields(
                item,
                (
                    "signal_id",
                    "document_id",
                    "opportunity_id",
                    "document_content_hash",
                    "source_text_sha256",
                    "fingerprint",
                ),
            )
            | {"evidence": _evidence(item.get("evidence", []))}
            for item in relation.evidence_snapshot
        ]
        if not {relation.project_id, relation.contract_id} <= opportunity_ids:
            missing["relation_endpoint_outside_scope"] += 1
        if not set(relation.evidence_signal_ids) <= signal_ids:
            missing["relation_evidence_outside_scope"] += 1
        add("relation", data)
    for call in sorted(graph.model_calls, key=lambda row: row["id"]):
        add("extraction_call_metadata", call | {"association": "document_only_not_exact_signal"})
    for opportunity in sorted(graph.opportunities, key=lambda row: row.id):
        data = _pick(opportunity, _OPPORTUNITY_FIELDS)
        data["embedding"] = _vector(opportunity.embedding, missing, "opportunity_embedding_missing")
        if opportunity.institution_code != institution_code:
            missing["cross_institution_membership_opportunity"] += 1
        add("opportunity", data, prediction=True)
    digest = hashlib.sha256()
    for record in records:
        digest.update(_line(record))
    counts = dict(sorted(Counter(record["entity"] for record in records).items()))
    return ForecastSnapshot(
        records,
        ForecastSnapshotReport(
            SNAPSHOT_VERSION,
            captured_at.astimezone(UTC).isoformat(),
            institution_code,
            code_revision.lower(),
            digest.hexdigest(),
            counts,
            dict(sorted(missing.items())),
        ),
    )


def write_forecast_snapshot(
    path: Path, snapshot: ForecastSnapshot, *, max_signals: int = 10000
) -> None:
    """Bounded, atomic output. Validation or write failures preserve the previous file."""
    if max_signals < 1 or snapshot.report.counts.get("signal", 0) > max_signals:
        raise SnapshotLimitError("Signal cap exceeded; no snapshot written")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.writelines(snapshot.iter_jsonl_bytes())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def export_forecast_snapshot(
    session: AsyncSession,
    path: Path,
    *,
    institution_code: str,
    code_revision: str,
    max_signals: int = 10000,
    settings: Settings | None = None,
) -> ForecastSnapshotReport:
    """Freeze current state only; a fresh session is required for read-only repeatable-read.

    There is deliberately no as-of argument. The observation date includes late ingestion;
    operator-attested revision and current configuration are not historical verification.
    """
    _validate_options(institution_code, code_revision, max_signals)
    if (
        session.in_transaction()
        or session.identity_map
        or session.new
        or session.dirty
        or session.deleted
    ):
        raise ValueError("Forecast snapshot requires a fresh session without an active transaction")
    async with session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        captured_at = now_utc()
        graph = await _read_graph(session, institution_code, max_signals)
        snapshot = assemble_forecast_snapshot(
            graph,
            institution_code=institution_code,
            code_revision=code_revision,
            captured_at=captured_at,
            settings=settings or get_settings(),
        )
    await asyncio.to_thread(write_forecast_snapshot, Path(path), snapshot, max_signals=max_signals)
    return snapshot.report


async def _read_graph(
    session: AsyncSession, institution_code: str, max_signals: int
) -> ForecastGraph:
    graph = ForecastGraph()
    institution = await session.get(InstitutionRow, institution_code)
    if institution is None:
        raise ValueError("institution_code does not exist in the registry")
    linked_opportunities = (
        select(OpportunitySignal.opportunity_id)
        .join(Signal)
        .where(Signal.institution_code == institution_code)
    )
    graph.opportunities = list(
        (
            await session.scalars(
                select(Opportunity)
                .where(
                    or_(
                        Opportunity.institution_code == institution_code,
                        Opportunity.id.in_(linked_opportunities),
                    )
                )
                .order_by(Opportunity.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    opp_ids = [row.id for row in graph.opportunities]
    linked_signals = select(OpportunitySignal.signal_id).where(
        OpportunitySignal.opportunity_id.in_(opp_ids)
    )
    graph.signals = list(
        (
            await session.scalars(
                select(Signal)
                .where(
                    or_(Signal.institution_code == institution_code, Signal.id.in_(linked_signals))
                )
                .order_by(Signal.id)
                .limit(max_signals + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(graph.signals) > max_signals:
        raise SnapshotLimitError("Signal cap exceeded; no snapshot written")
    signal_ids = [row.id for row in graph.signals]
    doc_ids = {row.document_id for row in graph.signals}
    graph.documents = list(
        (await session.scalars(select(Document).where(Document.id.in_(doc_ids)))).all()
    )
    graph.chunks = list(
        (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id.in_(doc_ids))
            )
        ).all()
    )
    graph.links = list(
        (
            await session.scalars(
                select(OpportunitySignal).where(OpportunitySignal.opportunity_id.in_(opp_ids))
            )
        ).all()
    )
    graph.reviews = list(
        (
            await session.scalars(select(ReviewItem).where(ReviewItem.signal_id.in_(signal_ids)))
        ).all()
    )
    graph.relations = list(
        (
            await session.scalars(
                select(OpportunityRelation).where(
                    or_(
                        OpportunityRelation.project_id.in_(opp_ids),
                        OpportunityRelation.contract_id.in_(opp_ids),
                    )
                )
            )
        ).all()
    )
    graph.sources = list(
        (
            await session.scalars(
                select(Source).where(Source.id.in_({row.source_id for row in graph.documents}))
            )
        ).all()
    )
    codes = {institution_code} | {
        row.institution_code for row in graph.signals + graph.documents + graph.opportunities
    }
    codes.update(row.speaker_institution_code for row in graph.signals)
    graph.institutions = list(
        (
            await session.scalars(
                select(InstitutionRow).where(
                    or_(InstitutionRow.code.in_(codes), InstitutionRow.executive_code.in_(codes))
                )
            )
        ).all()
    )
    calls = await session.execute(
        select(
            LLMCall.id,
            LLMCall.document_id,
            LLMCall.task,
            LLMCall.provider,
            LLMCall.model,
            LLMCall.prompt_version,
            LLMCall.status,
            LLMCall.served_by,
            LLMCall.created_at,
        ).where(LLMCall.document_id.in_(doc_ids), LLMCall.task == "extract")
    )
    graph.model_calls = [dict(row) for row in calls.mappings()]
    calibration = await session.scalar(
        select(EvalRun)
        .where(EvalRun.kind == "backtest")
        .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
        .limit(1)
    )
    if calibration is not None:
        values = calibration.metrics.get("calibration", {})
        graph.calibration = {
            "eval_run_id": calibration.id,
            "created_at": calibration.created_at,
            "method_version": calibration.metrics.get("method_version"),
            "current_method_eligible": calibration.metrics.get("method_version") == METHOD_VERSION,
            "values": {
                str(key): float(value)
                for key, value in values.items()
                if re.fullmatch(r"[a-z_]+:[a-z_]+", str(key)) and isinstance(value, (int, float))
            }
            if isinstance(values, dict)
            else {},
            "applied_to_stored_predictions_verified": False,
        }
    return graph
