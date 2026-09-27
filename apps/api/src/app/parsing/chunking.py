"""Document-type-aware chunking.

The unit of extraction is not "N tokens" but the unit in which a commitment is expressed:

* **Council minutes** → an *exchange*: a member's question plus the officials' answers that
  follow. The commitment ("반영하겠습니다") is in the answer, the subject is often only in the
  question, so they must stay together. Chair procedure lines become their own chunks and are
  dropped by triage.
* **Budget books** → a *세부사업 block*: the project line with its amounts plus the 산출기초 lines
  under it, labelled with the department heading above it.
* **Structured records** (발주계획/사전규격/입찰공고) → one chunk.

Chunks keep ``char_start``/``char_end`` into the stored document text so evidence spans found
by the verifier can be highlighted in the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_CHUNK_CHARS = 2400

_SPEAKER_RE = re.compile(
    r"^[○◯◎]\s*(?P<role>[가-힣A-Za-z·]{1,20}?)\s+(?P<name>[가-힣]{2,4})(?:\s{1,}|$)(?P<speech>.*)$"
)
_MEMBER_ROLES = ("위원", "의원")
_CHAIR_ROLES = ("위원장", "의장", "부의장")


@dataclass(slots=True)
class Chunk:
    seq: int
    char_start: int
    char_end: int
    text: str
    labels: list[str] = field(default_factory=list)
    kind: str = "text"  # exchange | procedure | statement | budget_line | record | text


@dataclass(slots=True)
class Turn:
    role: str
    name: str
    start: int
    end: int

    @property
    def is_member(self) -> bool:
        return self.role in _MEMBER_ROLES

    @property
    def is_chair(self) -> bool:
        return self.role in _CHAIR_ROLES


def split_turns(text: str) -> list[Turn]:
    turns: list[Turn] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        m = _SPEAKER_RE.match(stripped)
        line_end = offset + len(line.rstrip("\r\n"))
        if m:
            turns.append(
                Turn(m.group("role"), m.group("name"), offset + line.find(stripped[0]), line_end)
            )
        elif turns and stripped and not stripped.startswith("("):
            turns[-1].end = line_end  # continuation of the previous speaker
        offset += len(line)
    return turns


def chunk_minutes(text: str) -> list[Chunk]:
    turns = split_turns(text)
    chunks: list[Chunk] = []

    def emit(group: list[Turn], kind: str) -> None:
        start, end = group[0].start, group[-1].end
        labels = [f"{t.role} {t.name}" for t in group]
        # Very long speeches (구정질문) get split on sentence boundaries.
        while end - start > MAX_CHUNK_CHARS:
            cut = text.rfind(".", start, start + MAX_CHUNK_CHARS)
            cut = cut + 1 if cut > start + 200 else start + MAX_CHUNK_CHARS
            chunks.append(Chunk(len(chunks), start, cut, text[start:cut], labels, kind))
            start = cut
        chunks.append(Chunk(len(chunks), start, end, text[start:end], labels, kind))

    i = 0
    while i < len(turns):
        turn = turns[i]
        if turn.is_member:
            group = [turn]
            j = i + 1
            while j < len(turns) and not turns[j].is_member and not turns[j].is_chair:
                group.append(turns[j])
                j += 1
            emit(group, "exchange")
            i = j
        elif turn.is_chair:
            emit([turn], "procedure")
            i += 1
        else:
            emit([turn], "statement")
            i += 1
    if not chunks and text.strip():
        return chunk_plain(text)
    return chunks


_DEPT_RE = re.compile(r"^\s*부\s*서\s*[:：]\s*(?P<dept>\S.*?)\s*$")  # "부서: 분당구 건설과"
_PROJECT_RE = re.compile(
    r"^\s*(?:세\s*부\s*사\s*업\s*[:：]?\s*)?(?P<name>[가-힣A-Za-z0-9·()\-\s]{3,60}?)\s+"
    r"(?P<amount>\d{1,3}(?:,\d{3})+|\d{4,})(?:\s|$)"
)
_BASIS_RE = re.compile(r"^\s*(?:[∘ㅇ○o°·\-]|\d\))\s*")
# Real 세출예산사업명세서 rows (성남시, 2026): "306 출연금 2,900,000 …" is a 편성목, "01 출연금 …"
# a 통계목, and "도 113,293 …" a funding source (국·도·시비, 균특, 기금, 조정교부금).
_OBJECT_RE = re.compile(r"^\s*\d{3}\s+\S")
_ITEM_RE = re.compile(r"^\s*(?:\d{2,3}\s+\S|[국도시균기조특]\s+△?\d)")
_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d{4,}")
_AMOUNT_TAIL_RE = re.compile(r"\d\s*$")


def _is_project_row(lines: list[str], i: int) -> bool:
    """An unlabeled "name 예산액 전년도 증감" row. 부서·정책·단위사업 subtotals look the same;
    only a 세부사업 is followed (after its funding lines) by a 편성목 row."""
    content = lines[i]
    if not _PROJECT_RE.match(content) or _BASIS_RE.match(content) or _ITEM_RE.match(content):
        return False
    prev = next((ln for ln in reversed(lines[:i]) if ln.strip()), "")
    if _BASIS_RE.match(prev) and not _AMOUNT_TAIL_RE.search(prev):
        return False  # the tail of a wrapped "○…" basis line: " 및 컨설팅 44,935 65,375 △20,440"
    for nxt in lines[i + 1 :]:
        if not nxt.strip() or (_ITEM_RE.match(nxt) and not _OBJECT_RE.match(nxt)):
            continue  # blank or a funding line
        return bool(_OBJECT_RE.match(nxt))
    return False


def chunk_budget(text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    dept: str | None = None
    current: list[tuple[int, int]] = []
    current_dept: str | None = None
    offset = 0
    lines = [line.rstrip("\r\n") for line in text.splitlines(keepends=True)]

    def flush() -> None:
        nonlocal current
        if current:
            start, end = current[0][0], current[-1][1]
            labels = [f"부서: {current_dept}"] if current_dept else []
            chunks.append(Chunk(len(chunks), start, end, text[start:end], labels, "budget_line"))
        current = []

    for i, line in enumerate(text.splitlines(keepends=True)):
        content = lines[i]
        start, end = offset, offset + len(content)
        offset += len(line)
        if not content.strip():
            continue
        if m := _DEPT_RE.match(content):
            flush()
            dept = m.group("dept")
            continue
        # "세부사업" with an amount: a labeled row, not the "…ㆍ세부사업ㆍ편성목 예산액" header
        labeled = "세부사업" in content.replace(" ", "") and _AMOUNT_RE.search(content)
        if labeled or _is_project_row(lines, i):
            flush()
            current = [(start, end)]
            current_dept = dept
        elif current and (_BASIS_RE.match(content) or _ITEM_RE.match(content)):
            current.append((start, end))
        else:
            flush()
    flush()
    return chunks


def chunk_plain(text: str, size: int = 1500, overlap: int = 200) -> list[Chunk]:
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            para = text.rfind("\n", start + size // 2, end)
            if para > start:
                end = para
        chunks.append(Chunk(len(chunks), start, end, text[start:end]))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunk_document(doc_type: str, text: str) -> list[Chunk]:
    if doc_type == "council_minutes":
        return chunk_minutes(text)
    if doc_type == "budget_book":
        return chunk_budget(text)
    if doc_type in ("order_plan", "prespec", "bid_notice", "award"):
        return [Chunk(0, 0, len(text), text, kind="record")]
    return chunk_plain(text)
