"""Real arrival-sensitive components: determinism checks, not labeled match accuracy."""

import gzip
import json
from datetime import date
from functools import lru_cache
from itertools import permutations
from pathlib import Path

import pytest

from app.db.models import Signal
from app.domain.embedding import HashingEmbedder
from app.domain.synonyms import canonicalize
from app.pipeline.link_partition import plan_partition, stable_signal_key

TODAY = date(2026, 9, 29)
EXPORT = Path(__file__).resolve().parents[4] / "docs/data/seongnam-link-signals.jsonl.gz"
COMPONENTS = {
    "named_park": ("12b6493198", "75478dc1d5", "f183f8903d"),
    "generic_remodel": ("aa207f0049", "b80a2c5e48", "f87bd1d873"),
}
SUMMARY_FIELDS = (
    "title",
    "category",
    "department",
    "stage",
    "status",
    "first_seen_at",
    "last_signal_at",
    "signal_count",
    "est_budget_krw",
    "best_commitment",
    "conversion_prob",
    "bid_published_at",
    "bid_window_start",
    "bid_window_end",
)


@lru_cache(maxsize=1)
def real_components():
    wanted = tuple(prefix for prefixes in COMPONENTS.values() for prefix in prefixes)
    with gzip.open(EXPORT, "rt", encoding="utf-8") as stream:
        rows = [row for row in map(json.loads, stream) if row["key"].startswith(wanted)]
    assert len(rows) == 6
    embedder = HashingEmbedder()
    for row in rows:
        text = canonicalize(f"{row['title']} {row['summary']} {' '.join(row['keywords'])}")
        row["embedding"] = embedder.embed_one(text)
    return rows


def real_signals(component, ids):
    records = sorted(
        (row for row in real_components() if row["key"].startswith(COMPONENTS[component])),
        key=lambda row: row["key"],
    )
    signals, keys = [], {}
    fields = (
        "institution_code",
        "speaker_institution_code",
        "department",
        "title",
        "summary",
        "stage",
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
        "external_refs",
    )
    for position, (record, number) in enumerate(zip(records, ids, strict=True)):
        row = Signal(
            id=number,
            document_id=100 + number,
            chunk_id=200 + number,
            dedupe_key=f"id-dependent-dedupe-{number}",
            observed_at=date.fromisoformat(record["observed_at"]),
            embedding=list(record["embedding"]),
            evidence=[],
            **{name: record[name] for name in fields},
        )
        doc = record["document"]
        keys[number] = stable_signal_key(
            row,
            source_key="real-export",
            document_external_id=doc["external_id"],
            document_type=doc["doc_type"],
            chunk_seq=position,
        )
        signals.append(row)
    return signals, keys


def snapshot(rows, keys):
    groups = plan_partition(rows, keys, threshold=0.6, review_band=0.08, today=TODAY)
    return sorted(
        (
            tuple(sorted(keys[row.id] for row in group.members)),
            tuple(getattr(group.summary, name) for name in SUMMARY_FIELDS),
            tuple(group.summary.keywords),
            tuple(group.summary.embedding) if group.summary.embedding is not None else None,
        )
        for group in groups
    )


@pytest.mark.parametrize("component", COMPONENTS)
def test_real_changed_components_ignore_input_and_creation_order(component):
    expected = None
    for ids in permutations((1, 20, 300)):
        rows, keys = real_signals(component, ids)
        before = [(row.title, row.dedupe_key, list(row.embedding)) for row in rows]
        for incoming in permutations(rows):
            actual = snapshot(incoming, keys)
            if expected is None:
                expected = actual
            assert actual == expected
            assert [(row.title, row.dedupe_key, list(row.embedding)) for row in rows] == before


def test_late_same_book_department_evidence_ignores_input_and_creation_order():
    """A=(book1,X), B=(book2,Y), C=(book2,X) exposed a greedy irreversible join.

    Previously separate calls A/B/C yielded AB+C and B/C/A yielded B+AC. Replanning
    the same observed universe must have one result; this test supplies no truth labels.
    """
    expected = None
    for ids in permutations((1, 20, 300)):
        rows = []
        keys = {}
        for name, number, document, department in zip(
            ("A", "B", "C"), ids, (10, 20, 20), ("X", "Y", "X"), strict=True
        ):
            rows.append(
                Signal(
                    id=number,
                    document_id=document,
                    institution_code="LG-41130",
                    title="소규모 정비공사",
                    summary="시설 정비",
                    department=department,
                    stage="budget_line",
                    category="facility",
                    observed_at=date(2025, 12, 31),
                    verdict="accepted",
                    budget_krw=10_000_000,
                    embedding=[1.0, 0.0],
                    external_refs={},
                    keywords=["정비"],
                    evidence=[],
                    dedupe_key=f"id-derived-{number}",
                )
            )
            keys[number] = name
        for incoming in permutations(rows):
            # Exercise arrival prefixes too: the final call must reconsider the whole set.
            for count in (1, 2):
                snapshot(incoming[:count], keys)
            actual = snapshot(incoming, keys)
            if expected is None:
                expected = actual
            assert actual == expected
            assert all(
                not {keys[row.id] for row in group.members}.issuperset({"B", "C"})
                for group in plan_partition(
                    incoming, keys, threshold=0.6, review_band=0.08, today=TODAY
                )
            ), "Two distinct rows of one budget book must stay separate"
