"""Deterministic synthetic world: ten local governments, ~70 purchase lifecycles, ~300 documents.

Why synthetic data at all? The real sources need API keys that a reviewer will not have, and —
more importantly — real data has no ground truth. Here every document is generated *from* a
known opportunity timeline, so we can measure what matters:

* extraction precision/recall and field accuracy (gold quotes, budgets, years, commitment),
* linking quality (did 7 documents about one project end up in one opportunity?),
* backtest behaviour (how often does "검토하겠습니다" become a tender, and how early?).

Realism is deliberate where it makes the pipeline's job hard: spoken amounts are rounded
("3억 5천만원") while budget books carry exact 천원 figures; names drift across stages; institution
names come in four spellings; 30% of budget books are scanned images that need OCR; 20% of
tenders have no prior public signal; weak commitments usually never materialise.

Numbers produced from this world describe the pipeline on this world — they are not claims
about real-world accuracy. See docs/evaluation.md.
"""

from __future__ import annotations

import io
import json
import random
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from typing import Any
from xml.sax.saxutils import escape

from app.demo.archetypes import (
    ARCHETYPES,
    FILLER_TOPICS,
    GIVEN,
    OPERATING_LINES,
    ORDINANCES,
    SURNAMES,
    Archetype,
)
from app.domain.institutions import Institution, InstitutionRegistry, load_registry_csv
from app.domain.krw import find_amounts
from app.domain.timing import resolve_timing
from app.sources.base import RawRecord
from app.sources.g2b import map_item

DEMO_INSTITUTIONS = (
    "LG-11680",
    "LG-41130",
    "LG-41110",
    "LG-26350",
    "LG-30200",
    "LG-46150",
    "LG-47110",
    "LG-50000",
    "LG-28185",
    "LG-36110",
)

# The demo world is set before 2026-07-01, when 순천시 was 전라남도's. Its documents keep those
# spellings, which still resolve as the old names they are, and a seed keeps its world.
DEMO_SPELLINGS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "LG-46150": ("전라남도 순천시", "전라남도", ("순천시청", "순천시")),
    "CN-46150": ("전라남도 순천시의회", "전라남도", ("순천시의회",)),
}

COMMITTEES = ("행정자치위원회", "복지건설위원회", "경제도시위원회", "예산결산특별위원회")
_MATERIALIZE_P = {"committed": 0.88, "planned": 0.7, "reviewing": 0.28, "declined": 0.04}
_COMMITMENT_P = (("committed", 0.34), ("planned", 0.3), ("reviewing", 0.26), ("declined", 0.1))


@dataclass
class StageEvent:
    stage: str
    on: date
    doc_key: str  # "<source_key>:<external_id>" — filled when the document is emitted
    amount: int | None = None
    title: str | None = None


@dataclass
class OpportunityTruth:
    truth_id: str
    institution_code: str
    council_code: str
    archetype: str
    department: str
    qty: int
    amount: int
    commitment: str
    materializes: bool
    has_council_signal: bool
    bid_year: int | None
    bid_on: date | None
    events: list[StageEvent] = field(default_factory=list)


@dataclass
class GoldMention:
    """One signal a perfect extractor should produce from one document."""

    doc_key: str
    truth_id: str
    stage: str
    quote: str
    institution_code: str
    category: str
    budget_krw: int | None
    expected_year: int | None
    commitment: str | None


@dataclass
class SyntheticWorld:
    anchor: date
    seed: int
    truths: list[OpportunityTruth]
    records: dict[str, list[RawRecord]]
    gold: list[GoldMention]
    doc_truth: dict[str, list[str]]  # doc_key -> truth ids mentioned
    source_texts: dict[str, str] = field(default_factory=dict)  # pre-render text (OCR eval)

    def summary(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor.isoformat(),
            "seed": self.seed,
            "opportunities": len(self.truths),
            "materialized": sum(t.materializes for t in self.truths),
            "documents": {k: len(v) for k, v in self.records.items()},
            "gold_mentions": len(self.gold),
        }

    def truth_json(self) -> str:
        return json.dumps(
            {
                "summary": self.summary(),
                "truths": [asdict(t) for t in self.truths],
                "gold": [asdict(g) for g in self.gold],
                "doc_truth": self.doc_truth,
            },
            ensure_ascii=False,
            default=str,
            indent=1,
        )


# ------------------------------------------------------------------------------------------------
# Korean surface forms
# ------------------------------------------------------------------------------------------------
def spoken_krw(value: int) -> str:
    """Officials speak in round numbers: 352,000,000 → '3억 5천만원'."""
    rounded = round(value / 10_000_000) * 10_000_000 or round(value / 1_000_000) * 1_000_000
    eok, rest = divmod(rounded, 100_000_000)
    cheonman = rest // 10_000_000
    baekman = (rest % 10_000_000) // 1_000_000
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if cheonman:
        parts.append(f"{cheonman}천")
    if baekman and not eok:
        parts.append(f"{baekman}백")
    if not parts:
        return f"{value // 10_000:,}만원"
    text = " ".join(parts)
    return text + ("만원" if (cheonman or baekman) else "원")


def _has_batchim(word: str) -> bool:
    ch = word.rstrip(" )」")[-1:] or "가"
    code = ord(ch) - 0xAC00
    if 0 <= code < 11172:
        return code % 28 != 0
    if ch.isdigit():
        return ch in "013678"
    return ch.upper() in "LMNR"


def josa(word: str, pair: str) -> str:
    """Attach the right particle: josa("스마트폴", "을/를") -> "스마트폴을"."""
    with_b, without_b = pair.split("/")
    return word + (with_b if _has_batchim(word) else without_b)


def spoken_value(value: int) -> int:
    return round(value / 10_000_000) * 10_000_000 or round(value / 1_000_000) * 1_000_000


def _person(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + rng.choice(GIVEN) + rng.choice(GIVEN)


def _short_name(inst: Institution) -> str:
    return inst.sigungu or inst.name.split()[-1]


def _institution_spelling(inst: Institution, rng: random.Random) -> str:
    variants = [inst.name, *inst.aliases]
    if inst.sigungu:
        variants.append(f"{inst.sido} {inst.sigungu}청")
    return rng.choice(variants)


def _timing_phrase(meeting: date, bid_year: int, bid_on: date, rng: random.Random) -> str:
    half = "상반기" if bid_on.month <= 6 else "하반기"
    diff = bid_year - meeting.year
    if diff <= 0:
        return rng.choice([f"올해 {half}에", "이번 추경에", f"{half} 중에"])
    if diff == 1:
        return rng.choice(["내년도 본예산에", f"내년 {half}에", f"{bid_year}년도 본예산에"])
    return rng.choice([f"{bid_year}년에", f"'{bid_year % 100}년 {half}에"])


# ------------------------------------------------------------------------------------------------
# World generation
# ------------------------------------------------------------------------------------------------
class _Builder:
    def __init__(
        self,
        anchor: date,
        seed: int,
        registry: InstitutionRegistry,
        scale: float,
        scanned_ratio: float,
        render_pdfs: bool,
    ) -> None:
        self.anchor = anchor
        self.rng = random.Random(seed)
        self.seed = seed
        self.registry = registry
        self.scale = scale
        self.scanned_ratio = scanned_ratio
        self.render_pdfs = render_pdfs
        self.start = anchor - timedelta(days=int(30 * 30.4))
        self.truths: list[OpportunityTruth] = []
        self.gold: list[GoldMention] = []
        self.records: dict[str, list[RawRecord]] = {
            "fixture_minutes": [],
            "fixture_budget": [],
            "fixture_order_plan": [],
            "fixture_prespec": [],
            "fixture_bid": [],
        }
        self.doc_truth: dict[str, list[str]] = {}
        self.source_texts: dict[str, str] = {}
        self._seq = 0

    def _institution(self, code: str) -> Institution | None:
        inst = self.registry.get(code)
        if inst is None or code not in DEMO_SPELLINGS:
            return inst
        name, sido, aliases = DEMO_SPELLINGS[code]
        return replace(inst, name=name, sido=sido, aliases=aliases)

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    # -- timelines --------------------------------------------------------------------------
    def plan_truths(self) -> None:
        per_inst = max(2, round(7 * self.scale))
        n = 0
        for code in DEMO_INSTITUTIONS:
            inst = self._institution(code)
            assert inst is not None
            council = "CN-" + code.split("-", 1)[1]
            for arch in self.rng.sample(ARCHETYPES, per_inst):
                n += 1
                self.truths.append(self._plan_one(f"T-{n:03d}", inst, council, arch))

    def _plan_one(
        self, tid: str, inst: Institution, council: str, arch: Archetype
    ) -> OpportunityTruth:
        rng = self.rng
        qty = rng.randint(*arch.qty)
        amount = qty * rng.randint(*arch.unit_cost)
        amount = max(round(amount / 1_000_000) * 1_000_000, 30_000_000)
        commitment = rng.choices([c for c, _ in _COMMITMENT_P], [p for _, p in _COMMITMENT_P])[0]
        has_council = rng.random() > 0.2
        materializes = rng.random() < (_MATERIALIZE_P[commitment] if has_council else 1.0)
        t0 = self.start + timedelta(days=rng.randint(0, (self.anchor - self.start).days - 60))
        bid_on: date | None = None
        bid_year: int | None = None
        if materializes:
            bid_on = t0 + timedelta(days=rng.randint(200, 480))
            bid_year = bid_on.year
        truth = OpportunityTruth(
            tid,
            inst.code,
            council,
            arch.key,
            rng.choice(arch.departments),
            qty,
            amount,
            commitment,
            materializes,
            has_council,
            bid_year,
            bid_on,
        )
        if has_council:
            truth.events.append(StageEvent("council_mention", t0, ""))
            if materializes and rng.random() < 0.45:
                follow = t0 + timedelta(days=rng.randint(60, 150))
                if bid_on is None or follow < bid_on - timedelta(days=60):
                    truth.events.append(StageEvent("council_mention", follow, ""))
        if materializes and bid_on is not None:
            fy = bid_on.year
            supplementary = bid_on.month >= 8 and rng.random() < 0.35
            budget_on = date(fy, 7, 8) if supplementary else date(fy - 1, 12, 18)
            truth.events.append(
                StageEvent(
                    "budget_line",
                    budget_on,
                    "",
                    amount=int(round(amount * rng.uniform(0.97, 1.06), -6)),
                )
            )
            if rng.random() < 0.9:
                plan_on = bid_on - timedelta(days=rng.randint(30, 120))
                truth.events.append(StageEvent("order_plan", max(plan_on, budget_on), ""))
            if arch.procurement_type != "construction" and rng.random() < 0.75:
                truth.events.append(
                    StageEvent("prespec", bid_on - timedelta(days=rng.randint(12, 40)), "")
                )
            truth.events.append(StageEvent("bid_notice", bid_on, ""))
        truth.events.sort(key=lambda e: e.on)
        return truth

    # -- documents ---------------------------------------------------------------------------
    def emit_all(self) -> None:
        self._emit_minutes()
        self._emit_budget_books()
        self._emit_structured()
        self._emit_noise_structured()

    def _emit_minutes(self) -> None:
        rng = self.rng
        # Group council mentions by (council, month): one committee session per group.
        sessions: dict[tuple[str, str], list[tuple[OpportunityTruth, StageEvent, int]]] = {}
        for t in self.truths:
            mentions = [e for e in t.events if e.stage == "council_mention"]
            for idx, ev in enumerate(mentions):
                if ev.on > self.anchor:
                    continue
                sessions.setdefault((t.council_code, ev.on.strftime("%Y-%m")), []).append(
                    (t, ev, idx)
                )
        # Plus filler-only sessions so triage has something to throw away.
        for code in DEMO_INSTITUTIONS:
            council_key = "CN-" + code.split("-", 1)[1]
            for _ in range(max(1, round(3 * self.scale))):
                d = self.start + timedelta(days=rng.randint(0, (self.anchor - self.start).days))
                sessions.setdefault((council_key, d.strftime("%Y-%m")), [])
        session_no: dict[str, int] = {}
        for (council_code, month), items in sorted(sessions.items(), key=lambda kv: kv[0][1]):
            council_inst = self._institution(council_code)
            assert council_inst is not None
            session_no[council_code] = session_no.get(council_code, 270 + rng.randint(0, 40)) + 1
            day = min((ev.on for _, ev, _ in items), default=date.fromisoformat(month + "-15"))
            self._emit_session(council_inst, session_no[council_code], day, items)

    def _emit_session(
        self,
        council: Institution,
        session: int,
        day: date,
        items: list[tuple[OpportunityTruth, StageEvent, int]],
    ) -> None:
        rng = self.rng
        committee = rng.choice(COMMITTEES)
        chair = _person(rng)
        ext = f"MIN-{council.code}-{day:%Y%m%d}-{self._next():04d}"
        doc_key = f"fixture_minutes:{ext}"
        lines = [
            f"제{session}회 {council.name.split()[-1]} 임시회",
            f"{committee} 회의록",
            f"제{rng.randint(1, 4)}차",
            f"일시: {day.year}년 {day.month}월 {day.day}일({'월화수목금'[day.weekday() % 5]}) 10시 00분",
            "장소: 위원회 회의실",
            "",
            f"(10시 0{rng.randint(1, 9)}분 개의)",
            f"○위원장 {chair}  의석을 정돈하여 주시기 바랍니다. 성원이 되었으므로 "
            f"제{session}회 임시회 제{rng.randint(1, 4)}차 {committee}를 개의하겠습니다.",
            f"○위원장 {chair}  의사일정 제1항 {rng.choice(ORDINANCES)} 일부개정조례안을 상정합니다. "
            "제안설명은 서면으로 대체하겠습니다.",
        ]
        exchanges: list[tuple[str, str, OpportunityTruth | None, StageEvent | None, int]] = []
        for q, a in rng.sample(FILLER_TOPICS, k=min(len(FILLER_TOPICS), rng.randint(2, 4))):
            exchanges.append((q, a, None, None, 0))
        for truth, ev, idx in items:
            q, a = self._mention_text(truth, ev, idx)
            exchanges.append((q, a, truth, ev, idx))
        rng.shuffle(exchanges)
        text_so_far = "\n".join(lines)
        body: list[str] = []
        mentioned: list[str] = []
        for q, a, ex_truth, ex_event, _idx in exchanges:
            member = _person(rng)
            q_line = f"○위원 {member}  {q}"
            if ex_truth is not None:
                official = f"○{ex_truth.department}장 {_person(rng)}"
            else:
                role = rng.choice(["총무과장", "기획예산과장", "안전총괄과장", "민원여권과장"])
                official = f"○{role} {_person(rng)}"
            a_line = f"{official}  {a}"
            body += [q_line, a_line]
            if ex_truth is not None and ex_event is not None:
                ex_event.doc_key = doc_key
                mentioned.append(ex_truth.truth_id)
                self._gold_for_mention(doc_key, ex_truth, ex_event, a)
        closing = [
            f"○위원장 {chair}  더 질의하실 위원님 안 계십니까? (「없습니다」 하는 위원 있음)",
            f"○위원장 {chair}  이상으로 오늘 회의를 모두 마치겠습니다. 산회를 선포합니다.",
            f"(12시 {rng.randint(10, 50)}분 산회)",
        ]
        text = text_so_far + "\n" + "\n".join(body + closing)
        self.doc_truth[doc_key] = mentioned
        self.records["fixture_minutes"].append(
            RawRecord(
                external_id=ext,
                doc_type="council_minutes",
                title=f"제{session}회 {council.name} 임시회 {committee} 회의록",
                published_at=day + timedelta(days=rng.randint(14, 40)),
                mime="text/plain; charset=utf-8",
                publisher_raw=council.name,
                institution_code_hint=council.code,
                url=f"https://clik.nanet.go.kr/synthetic/{ext}",
                content=text.encode("utf-8"),
                structured={
                    "meeting_date": day.isoformat(),
                    "council": council.name,
                    "synthetic": True,
                },
            )
        )

    def _mention_text(self, t: OpportunityTruth, ev: StageEvent, idx: int) -> tuple[str, str]:
        rng = self.rng
        arch = _arch(t.archetype)
        inst = self._institution(t.institution_code)
        assert inst is not None
        spoken = rng.choice(arch.spoken)
        place = f"{_short_name(inst)} " + rng.choice(
            ["주민들께서", "어르신들께서", "학부모님들께서"]
        )
        if idx == 0:
            q = rng.choice(
                [
                    f"{place} {arch.pain} 말씀을 많이 하십니다. {josa(spoken, '을/를')} 도입할 계획은 없습니까?",
                    f"다른 지자체에서는 {josa(spoken, '을/를')} 운영해서 반응이 좋다고 들었습니다. "
                    f"우리 {_short_name(inst)}도 추진할 의향이 있는지 묻고 싶습니다.",
                    f"{arch.pain} 민원이 계속 들어옵니다. {spoken} 같은 대책이 필요하지 않겠습니까?",
                ]
            )
        else:
            q = f"지난번에 말씀드렸던 {spoken} 건은 어떻게 진행되고 있습니까?"
        level = t.commitment if idx == 0 else "committed"
        budget_phrase = spoken_krw(t.amount)
        qty_phrase = f"{t.qty}{arch.unit}" if arch.unit != "식" else ""
        name = rng.choice(arch.budget_names)
        if level in ("committed", "planned") and t.bid_on and t.bid_year:
            timing = _timing_phrase(ev.on, t.bid_year, t.bid_on, rng)
        else:
            timing = rng.choice(["내년에", "중장기적으로"])
        if level == "committed":
            a = rng.choice(
                [
                    f"네, 위원님 말씀에 공감합니다. {timing} {name} 사업비 {budget_phrase}을 "
                    f"반영하겠습니다.",
                    f"{timing} {qty_phrase + ' 규모로 ' if qty_phrase else ''}{name} 예산 "
                    f"{josa(budget_phrase, '을/를')} "
                    f"편성할 계획이며, 확정되는 대로 발주할 예정입니다.",
                ]
            )
        elif level == "planned":
            a = (
                f"현재 {name} 기본계획을 수립하고 있으며, {timing} 추진할 계획입니다. "
                f"사업비는 약 {budget_phrase} 정도로 예상하고 있습니다."
            )
        elif level == "reviewing":
            a = rng.choice(
                [
                    f"좋은 제안 감사합니다. {josa(name, '은/는')} 예산 여건과 타 지자체 사례를 "
                    "면밀히 검토하겠습니다.",
                    f"{josa(spoken, '은/는')} 필요성은 공감합니다만, 효과성 검증이 필요해서 "
                    "적극 검토하겠습니다.",
                ]
            )
        else:
            a = f"취지는 공감하나 현재 재정 여건상 {josa(name, '은/는')} 당분간 추진하기 어렵습니다."
        return q, a

    def _gold_for_mention(
        self, doc_key: str, t: OpportunityTruth, ev: StageEvent, answer: str
    ) -> None:
        arch = _arch(t.archetype)
        committed = any(p in answer for p in ("반영하겠습니다", "편성할 계획", "발주할 예정"))
        level = (
            "committed"
            if committed
            else (
                "planned"
                if "계획입니다" in answer
                else "reviewing"
                if "검토하겠습니다" in answer
                else "declined"
            )
        )
        has_amount = bool(find_amounts(answer))
        # Gold is faithful to the *text*: a committed-but-never-bought project still has the
        # year the official said out loud.
        year: int | None = None
        if level in ("committed", "planned"):
            resolved = resolve_timing(answer, ev.on)
            year = resolved.year if resolved else None
        self.gold.append(
            GoldMention(
                doc_key=doc_key,
                truth_id=t.truth_id,
                stage="council_mention",
                quote=answer,
                institution_code=t.institution_code,
                category=arch.category.value,
                budget_krw=spoken_value(t.amount) if has_amount else None,
                expected_year=year,
                commitment=level,
            )
        )

    def _emit_budget_books(self) -> None:
        books: dict[tuple[str, int, str], list[tuple[OpportunityTruth, StageEvent]]] = {}
        for t in self.truths:
            for ev in t.events:
                if ev.stage == "budget_line" and ev.on <= self.anchor:
                    kind = "제1회 추가경정" if ev.on.month == 7 else "본"
                    fy = ev.on.year if kind != "본" else ev.on.year + 1
                    books.setdefault((t.institution_code, fy, kind), []).append((t, ev))
        # Every institution publishes a 본예산 each year even with no new projects in it.
        for code in DEMO_INSTITUTIONS:
            for fy in range(self.start.year + 1, self.anchor.year + 1):
                books.setdefault((code, fy, "본"), [])
        for (code, fy, kind), items in sorted(books.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            inst = self._institution(code)
            assert inst is not None
            published = date(fy, 7, 8) if kind != "본" else date(fy - 1, 12, 18)
            if published > self.anchor:
                continue
            self._emit_book(inst, fy, kind, published, items)

    def _emit_book(
        self,
        inst: Institution,
        fy: int,
        kind: str,
        published: date,
        items: list[tuple[OpportunityTruth, StageEvent]],
    ) -> None:
        rng = self.rng
        ext = f"BGT-{inst.code}-{fy}-{'S1' if kind != '본' else 'M'}"
        doc_key = f"fixture_budget:{ext}"
        header = [
            f"{fy}년도 {inst.name} 세출예산 사업명세서 ({kind}예산)",
            "(단위: 천원)",
        ]
        rows: list[tuple[str, list[str], OpportunityTruth | None, int]] = []
        for dept, name, (lo, hi) in rng.sample(OPERATING_LINES, k=rng.randint(4, 6)):
            amount_k = rng.randint(lo, hi)
            rows.append(
                (
                    dept,
                    [
                        f"세부사업: {name}  {amount_k:,}  {int(amount_k * 0.95):,}  "
                        f"{amount_k - int(amount_k * 0.95):,}",
                        f"  ㅇ {name} 운영 {amount_k:,}",
                    ],
                    None,
                    amount_k,
                )
            )
        for t, ev in items:
            arch = _arch(t.archetype)
            name = rng.choice(arch.budget_names)
            amount_k = int((ev.amount or t.amount) // 1000)
            unit_k = max(amount_k // max(t.qty, 1), 1)
            basis = (
                f"  ㅇ {name} {t.qty}{arch.unit} × {unit_k:,}천원 = {unit_k * t.qty:,}"
                if arch.unit != "식"
                else f"  ㅇ {name} 사업비 1식 {amount_k:,}"
            )
            line = f"세부사업: {name}  {amount_k:,}  0  {amount_k:,}"
            rows.append((t.department, [line, basis], t, amount_k))
            ev.doc_key = doc_key
            ev.title = name
            self.gold.append(
                GoldMention(
                    doc_key=doc_key,
                    truth_id=t.truth_id,
                    stage="budget_line",
                    quote=line,
                    institution_code=t.institution_code,
                    category=arch.category.value,
                    budget_krw=amount_k * 1000,
                    expected_year=fy,
                    commitment="committed",
                )
            )
        rng.shuffle(rows)
        body: list[str] = []
        for dept, lines, _t, _a in sorted(rows, key=lambda r: r[0]):
            body.append(f"부서: {dept}")
            body += lines
        text = "\n".join(header + body)
        self.doc_truth[doc_key] = [t.truth_id for t, _ in items]
        self.source_texts[doc_key] = text
        roll = rng.random()
        if self.render_pdfs and roll < self.scanned_ratio:
            content, mime = render_scanned_pdf(text, rng), "application/pdf"
        elif self.render_pdfs and roll < 0.8:
            content, mime = render_text_pdf(text), "application/pdf"
        else:
            content, mime = render_hwpx(text), "application/hwp+zip"
        self.records["fixture_budget"].append(
            RawRecord(
                external_id=ext,
                doc_type="budget_book",
                title=f"{fy}년도 {inst.name} {kind}예산서",
                published_at=published,
                mime=mime,
                publisher_raw=inst.name,
                institution_code_hint=inst.code,
                url=f"https://lofin.mois.go.kr/synthetic/{ext}",
                content=content,
                structured={"fiscal_year": fy, "budget_kind": kind, "synthetic": True},
            )
        )

    def _emit_structured(self) -> None:
        rng = self.rng
        for t in self.truths:
            arch = _arch(t.archetype)
            inst = self._institution(t.institution_code)
            assert inst is not None
            order_no = prespec_no = None
            for ev in t.events:
                if ev.on > self.anchor or ev.stage in ("council_mention", "budget_line"):
                    continue
                spelled = _institution_spelling(inst, rng)
                amount = int(round(t.amount * rng.uniform(0.95, 1.05), -5))
                formal = rng.choice(arch.formal_names)
                year_prefix = f"{ev.on.year if ev.stage != 'order_plan' else t.bid_year}년 "
                if ev.stage == "order_plan":
                    order_no = f"R{ev.on:%y}DD{self._next():08d}"
                    item = {
                        "orderPlanUntyNo": order_no,
                        "bizNm": (year_prefix if rng.random() < 0.6 else "") + formal,
                        "orderInsttNm": spelled,
                        "deptNm": t.department,
                        "sumOrderAmt": str(amount),
                        "orderYear": str(t.bid_year),
                        "orderMnth": f"{t.bid_on.month:02d}" if t.bid_on else "",
                        "cntrctMthdNm": rng.choice(["협상에의한계약", "제한경쟁", "일반경쟁"]),
                        "nticeDt": f"{ev.on:%Y-%m-%d} 09:00:00",
                        "ofclNm": _person(rng),
                    }
                    key = "fixture_order_plan"
                elif ev.stage == "prespec":
                    prespec_no = f"R{ev.on:%y}BD{self._next():08d}"
                    item = {
                        "bfSpecRgstNo": prespec_no,
                        "prdctClsfcNoNm": formal,
                        "rlDminsttNm": spelled,
                        "orderInsttNm": spelled,
                        "asignBdgtAmt": str(amount),
                        "rcptDt": f"{ev.on:%Y-%m-%d} 10:00:00",
                        "opninRgstClseDt": f"{ev.on + timedelta(days=5):%Y-%m-%d} 18:00:00",
                        "orderPlanUntyNo": order_no if order_no and rng.random() < 0.75 else "",
                    }
                    key = "fixture_prespec"
                else:
                    prefix = "[긴급] " if rng.random() < 0.1 else ""
                    suffix = (
                        " (협상에 의한 계약)"
                        if arch.procurement_type == "service" and rng.random() < 0.4
                        else ""
                    )
                    bid_no = f"R{ev.on:%y}BK{self._next():08d}"
                    item = {
                        "bidNtceNo": bid_no,
                        "bidNtceOrd": "000",
                        "bidNtceNm": f"{prefix}{year_prefix if rng.random() < 0.5 else ''}{formal}{suffix}",
                        "dminsttNm": spelled,
                        "ntceInsttNm": rng.choice([spelled, "조달청"]),
                        "asignBdgtAmt": str(amount),
                        "presmptPrce": str(int(amount / 1.1)),
                        "bidNtceDt": f"{ev.on:%Y-%m-%d} 14:00:00",
                        "bidClseDt": f"{ev.on + timedelta(days=14):%Y-%m-%d} 10:00:00",
                        "bfSpecRgstNo": prespec_no if prespec_no and rng.random() < 0.7 else "",
                        "bidNtceDtlUrl": f"https://www.g2b.go.kr/synthetic/{bid_no}",
                    }
                    key = "fixture_bid"
                rec = map_item(_DOC_TYPE_BY_KEY[key], item)
                assert rec is not None
                rec.structured["synthetic"] = True
                rec.structured["truth_id"] = t.truth_id  # never read by the pipeline; eval only
                ev.doc_key = f"{key}:{rec.external_id}"
                ev.title = rec.title
                ev.amount = amount
                self.records[key].append(rec)
                self.doc_truth[ev.doc_key] = [t.truth_id]

    def _emit_noise_structured(self) -> None:
        rng = self.rng
        mundane = (
            "청사 청소용역",
            "사무용품 구매",
            "하수관로 정비공사",
            "공용차량 임차",
            "청사 경비용역",
            "보도블록 정비공사",
            "민방위 교육장 임차",
            "홍보물 제작",
        )
        for _ in range(max(4, round(40 * self.scale))):
            inst = self._institution(rng.choice(DEMO_INSTITUTIONS))
            assert inst is not None
            on = self.start + timedelta(days=rng.randint(0, (self.anchor - self.start).days))
            no = f"R{on:%y}BK{self._next():08d}"
            amount = rng.randint(20, 400) * 1_000_000
            item = {
                "bidNtceNo": no,
                "bidNtceOrd": "000",
                "bidNtceNm": f"{on.year}년 {rng.choice(mundane)}",
                "dminsttNm": _institution_spelling(inst, rng),
                "asignBdgtAmt": str(amount),
                "bidNtceDt": f"{on:%Y-%m-%d} 11:00:00",
            }
            rec = map_item("bid_notice", item)
            assert rec is not None
            rec.structured["synthetic"] = True
            self.records["fixture_bid"].append(rec)
            self.doc_truth[f"fixture_bid:{rec.external_id}"] = []

        # 발주계획 filed under a bare "중구청"/"동구" with no 기관코드: six 광역시 have a 중구, so the
        # resolver must refuse to guess and the signal lands in the operator review queue.
        ambiguous = ("중구청", "중구", "동구청", "동구")
        interesting = (
            "스마트 버스정류장 설치 사업",
            "공영주차장 주차관제시스템 구축",
            "경로당 스마트 돌봄 플랫폼 구축",
            "하천 수위 원격감시 시스템 구축",
        )
        for _ in range(max(2, round(4 * self.scale))):
            on = self.anchor - timedelta(days=rng.randint(3, 60))
            no = f"R{on:%y}DD{self._next():08d}"
            item = {
                "orderPlanUntyNo": no,
                "bizNm": f"{on.year + 1}년 {rng.choice(interesting)}",
                "orderInsttNm": rng.choice(ambiguous),
                "sumOrderAmt": str(rng.randint(3, 30) * 50_000_000),
                "orderYear": str(on.year + 1),
                "orderMnth": f"{rng.randint(2, 6):02d}",
                "cntrctMthdNm": "제한경쟁",
                "nticeDt": f"{on:%Y-%m-%d} 09:00:00",
            }
            rec = map_item("order_plan", item)
            assert rec is not None
            rec.structured["synthetic"] = True
            self.records["fixture_order_plan"].append(rec)
            self.doc_truth[f"fixture_order_plan:{rec.external_id}"] = []


_DOC_TYPE_BY_KEY: dict[str, Any] = {
    "fixture_order_plan": "order_plan",
    "fixture_prespec": "prespec",
    "fixture_bid": "bid_notice",
}


def _arch(key: str) -> Archetype:
    from app.demo.archetypes import BY_KEY

    return BY_KEY[key]


# ------------------------------------------------------------------------------------------------
# Rendering (PDF with text layer, scanned PDF, HWPX)
# ------------------------------------------------------------------------------------------------
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _font_path() -> str | None:
    import os

    return next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)


def render_text_pdf(text: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font = _font_path()
    if font is None:
        return text.encode("utf-8")
    if "Nanum" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Nanum", font))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 60
    for line in text.splitlines():
        if y < 60:
            c.showPage()
            y = height - 60
        c.setFont("Nanum", 10)
        c.drawString(50, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


def render_scanned_pdf(text: str, rng: random.Random, dpi: int = 200) -> bytes:
    """Rasterise to a slightly skewed, noisy grayscale 'scan' with no text layer."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    font_path = _font_path()
    if font_path is None:
        return text.encode("utf-8")
    font = ImageFont.truetype(font_path, size=int(dpi / 72 * 10.5))
    width, height = int(8.27 * dpi), int(11.69 * dpi)
    line_h = int(font.size * 1.55)
    lines = text.splitlines()
    per_page = (height - 2 * int(0.8 * dpi)) // line_h
    pages = []
    for start in range(0, len(lines), per_page):
        img = Image.new("L", (width, height), color=248)
        draw = ImageDraw.Draw(img)
        y = int(0.8 * dpi)
        for line in lines[start : start + per_page]:
            draw.text((int(0.7 * dpi), y), line, fill=rng.randint(20, 45), font=font)
            y += line_h
        img = img.rotate(rng.uniform(-0.6, 0.6), fillcolor=248, resample=Image.Resampling.BICUBIC)
        img = img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.3, 0.7)))
        noise = Image.effect_noise((width, height), rng.uniform(6, 12))
        img = Image.blend(img, noise.convert("L"), 0.06)
        pages.append(img.convert("RGB"))
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=dpi)
    return buf.getvalue()


def render_hwpx(text: str) -> bytes:
    """Minimal HWPX (OWPML) package: enough structure for our reader, like real exports."""
    paras = "".join(
        f"<hp:p><hp:run><hp:t>{escape(line)}</hp:t></hp:run></hp:p>" for line in text.splitlines()
    )
    section = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
        'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">' + paras + "</hs:sec>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/section0.xml", section)
        zf.writestr("version.xml", '<?xml version="1.0"?><hv:HCFVersion xmlns:hv="x"/>')
    return buf.getvalue()


# ------------------------------------------------------------------------------------------------
def build_world(
    *,
    anchor: date,
    seed: int = 7,
    scale: float = 1.0,
    scanned_ratio: float = 0.3,
    render_pdfs: bool = True,
    registry: InstitutionRegistry | None = None,
) -> SyntheticWorld:
    b = _Builder(anchor, seed, registry or load_registry_csv(), scale, scanned_ratio, render_pdfs)
    b.plan_truths()
    b.emit_all()
    return SyntheticWorld(anchor, seed, b.truths, b.records, b.gold, b.doc_truth, b.source_texts)
