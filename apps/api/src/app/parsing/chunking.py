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
# A chair turn this short inside an exchange hands the floor over; it does not end the exchange.
_CHAIR_HANDOVER_CHARS = 80

_SPEAKER_RE = re.compile(
    r"^[○◯◎]\s*(?P<role>[가-힣A-Za-z·]{1,20}?)\s+(?P<name>[가-힣]{2,4})(?:\s{1,}|$)(?P<speech>.*)$"
)
# 성남시의회 (and other councils' HWP minutes) print members as "○조우현위원  질문": the name glued
# to 위원/의원, then two spaces. Officials keep "○교통도로국장 유동  답변". Without two spaces after
# the role, "○전문위원 홍길동" stays a role followed by a name.
GLUED_MEMBER_RE = re.compile(
    r"^[○◯◎]\s*(?P<name>[가-힣]{2,4})(?P<role>위원|의원)(?:\s{2,}|$)(?P<speech>.*)$"
)
# Most councils in CLIK (109 of 178 minutes from 52 councils, 2026-09-28) put a space between:
# "○최광선 의원  존경하는…", "○오문섭 위원 사실 오늘…", "○김석환 의원" (speech on the next line).
# The space is what tells "○박재신 위원" (a member) from "○위원장 박재신" and "○전문위원 홍길동".
SPACED_MEMBER_RE = re.compile(
    r"^[○◯◎]\s*(?!(?:출석|전문)\s)(?P<name>[가-힣]{2,4})\s(?P<role>위원|의원)(?:\s*:\s*|\s+|$)"
    r"(?P<speech>.*)$"
)


def match_member(line: str) -> re.Match[str] | None:
    """A council member's speaker line, in either spelling; groups ``name``, ``role``, ``speech``."""
    return GLUED_MEMBER_RE.match(line) or SPACED_MEMBER_RE.match(line)


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


# Some councils print no ○ at all (CLIK, 2026-09): 곡성군의회 writes "위원장 김홍순" alone on a
# line, 서초구의회 the role and the name on lines of their own ("위원장" / "신정태",
# "김해바른" / "위원"). These are read only when the ○ forms find (almost) nothing, so a document
# that uses ○ is split exactly as before.
_ROLE = (
    r"(?:위원장|부위원장|위원|의원|의장|부의장|전문위원|"
    r"[가-힣]{1,14}?(?:국장|과장|소장|팀장|실장|단장|본부장|센터장|담당관|원장|청장|직무대리))"
)
_BARE_SPEAKER_RE = re.compile(rf"^(?P<role>{_ROLE})\s+(?P<name>[가-힣]{{2,4}})$")
_BARE_ROLE_RE = re.compile(rf"^(?P<role>{_ROLE})$")
_BARE_NAME_RE = re.compile(r"^(?P<name>[가-힣]{2,4})$")
_BARE_MEMBER_RE = re.compile(r"^(?P<role>위원|의원)$")


def _bare_turns(text: str) -> list[Turn]:
    lines: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        lines.append(
            (
                offset + line.find(stripped[0]) if stripped else offset,
                offset + len(line.rstrip("\r\n")),
                stripped,
            )
        )
        offset += len(line)
    turns: list[Turn] = []
    i = 0
    while i < len(lines):
        start, end, stripped = lines[i]
        nxt = lines[i + 1][2] if i + 1 < len(lines) else ""
        m = _BARE_SPEAKER_RE.match(stripped)
        if m:
            turns.append(Turn(m.group("role"), m.group("name"), start, end))
            i += 1
            continue
        role, name = _BARE_ROLE_RE.match(stripped), _BARE_NAME_RE.match(nxt)
        if role and name:  # "위원장" / "신정태"
            turns.append(Turn(role.group("role"), name.group("name"), start, lines[i + 1][1]))
            i += 2
            continue
        name, role = _BARE_NAME_RE.match(stripped), _BARE_MEMBER_RE.match(nxt)
        if name and role:  # "김해바른" / "위원"
            turns.append(Turn(role.group("role"), name.group("name"), start, lines[i + 1][1]))
            i += 2
            continue
        if turns and stripped and not stripped.startswith("("):
            turns[-1].end = end
        i += 1
    return turns


def split_turns(text: str) -> list[Turn]:
    turns = _marked_turns(text)
    if len(turns) < 3:
        bare = _bare_turns(text)
        if len(bare) > len(turns):
            return bare
    return turns


def _marked_turns(text: str) -> list[Turn]:
    turns: list[Turn] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        m = match_member(stripped) or _SPEAKER_RE.match(stripped)
        line_end = offset + len(line.rstrip("\r\n"))
        if m:
            turns.append(
                Turn(m.group("role"), m.group("name"), offset + line.find(stripped[0]), line_end)
            )
        elif turns and stripped and not stripped.startswith("("):
            turns[-1].end = line_end  # continuation of the previous speaker
        offset += len(line)
    return turns


def _hands_over(turns: list[Turn], j: int) -> bool:
    after = turns[j + 1] if j + 1 < len(turns) else None
    return (
        turns[j].end - turns[j].start <= _CHAIR_HANDOVER_CHARS
        and after is not None
        and not after.is_member
        and not after.is_chair
    )


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
            # A short line that ends the piece ("주요사업비 예산 반영 내역입니다.") introduces what
            # follows, so the next piece starts with it too.
            lead = text.rfind("\n", start, cut) + 1
            start = lead if lead > start and len(text[lead:cut].strip()) <= 60 else cut
        chunks.append(Chunk(len(chunks), start, end, text[start:end], labels, kind))

    i = 0
    while i < len(turns):
        turn = turns[i]
        if turn.is_member:
            group = [turn]
            j = i + 1
            while j < len(turns) and not turns[j].is_member:
                # The chair handing the floor over ("과장님 답변하여 주시기 바랍니다") sits between
                # the question and its answer: a short chair turn followed by an official's
                # answer stays in the exchange. Any other chair turn ends it.
                if turns[j].is_chair and not _hands_over(turns, j):
                    break
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
_PROJECT_LABEL_RE = re.compile(r"^\s*세\s*부\s*사\s*업\s*[:：]?\s*")
_CELL_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_PROJECT_ROW_RE = re.compile(
    rf"^(?P<name>[가-힣A-Za-z0-9(].*?)\s+(?P<amount>{_CELL_NUMBER})"
    rf"\s+{_CELL_NUMBER}\s+[△−-]?{_CELL_NUMBER}\s*$"
)
_LABELED_AMOUNT_RE = re.compile(
    rf"^(?P<name>[가-힣A-Za-z0-9(].*?)\s+(?P<amount>{_CELL_NUMBER})\s*$"
)
_BASIS_RE = re.compile(r"^\s*(?:[∘ㅇ○o°·\-]|\d\))\s*")
# Real 세출예산사업명세서 rows (성남시, 2026): "306 출연금 2,900,000 …" is a 편성목, "01 출연금 …"
# a 통계목, and "도 113,293 …" a funding source (국·도·시비, 균특, 기금, 조정교부금).
_OBJECT_RE = re.compile(r"^\s*\d{3}\s+\S")
_ITEM_RE = re.compile(r"^\s*(?:\d{2,3}\s+\S|[국도시균기조특]\s+△?\d)")
_AMOUNT_TAIL_RE = re.compile(r"\d\s*$")


def budget_project_row(line: str) -> tuple[str, str] | None:
    """Read a project name and current-budget cell, never column headings or account rows.

    This is syntactic only: unlabeled rows still need structural context in the chunker
    to distinguish projects from department/policy subtotals. Keep source text untouched.
    """
    normalized = line.replace("|", " ").strip()
    labeled = _PROJECT_LABEL_RE.match(normalized)
    body = normalized[labeled.end() :] if labeled else normalized
    if _BASIS_RE.match(body) or _ITEM_RE.match(body):
        return None
    match = _PROJECT_ROW_RE.fullmatch(body)
    if match is None and labeled:
        match = _LABELED_AMOUNT_RE.fullmatch(body)
    if match is None:
        return None
    name = match.group("name").strip()
    if len(name) < 2 or name in {"세부사업", "사업명", "예산액", "합계", "소계", "총계"}:
        return None
    return name, match.group("amount")


def _project_table_header(line: str) -> bool:
    cells = [cell.strip().replace(" ", "") for cell in line.strip().strip("|").split("|")]
    return (
        len(cells) > 1
        and cells[0] in {"세부사업", "사업명"}
        and any("예산액" in cell for cell in cells[1:])
    )


def _is_project_row(lines: list[str], i: int) -> bool:
    """An unlabeled "name 예산액 전년도 증감" row. 부서·정책·단위사업 subtotals look the same;
    only a 세부사업 is followed (after its funding lines) by a 편성목 row."""
    content = lines[i]
    if budget_project_row(content) is None:
        return False
    prev = next((ln for ln in reversed(lines[:i]) if ln.strip()), "")
    if content[:1].isspace() and _BASIS_RE.match(prev) and not _AMOUNT_TAIL_RE.search(prev):
        return False  # the tail of a wrapped "○…" basis line: " 및 컨설팅 44,935 65,375 △20,440"
    for nxt in lines[i + 1 :]:
        if not nxt.strip() or (_ITEM_RE.match(nxt) and not _OBJECT_RE.match(nxt)):
            continue  # blank or a funding line
        return bool(_OBJECT_RE.match(nxt))
    return False


def chunk_budget(text: str, *, standalone: bool = False) -> list[Chunk]:
    chunks: list[Chunk] = []
    dept: str | None = None
    current: list[tuple[int, int]] = []
    current_dept: str | None = None
    offset = 0
    lines = [line.rstrip("\r\n") for line in text.splitlines(keepends=True)]
    first_content = next((i for i, line in enumerate(lines) if line.strip()), -1)
    project_table = False

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
            project_table = False
            continue
        if _project_table_header(content):
            flush()
            project_table = True
            continue
        normalized = content.replace("|", " ").strip()
        row = budget_project_row(content)
        labeled = bool(_PROJECT_LABEL_RE.match(normalized))
        # Extraction can receive an already-isolated unlabeled row. Only the first line
        # gets that allowance; full books still require a label/header or following account.
        isolated = standalone and i == first_content
        if row and (
            labeled or (project_table and "|" in content) or isolated or _is_project_row(lines, i)
        ):
            flush()
            current = [(start, end)]
            current_dept = dept
        elif current and (_BASIS_RE.match(normalized) or _ITEM_RE.match(normalized)):
            current.append((start, end))
        else:
            flush()
            if "|" not in content:
                project_table = False
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
