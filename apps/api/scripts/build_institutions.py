"""Rebuild ``src/app/domain/data/institutions.csv`` with every 지방자치단체, its council and every
시도 교육청, from a snapshot of 행정안전부 법정동코드.

Source: 공공데이터포털 "행정안전부_법정동코드" (``1741000/StanReginCd/getStanReginCdList``,
``flag=Y``: codes in force), pulled 2026-09-27. ``scripts/data/stan_regin_cd_2026-09-27.json``
keeps its 시도 and 시군구 rows, 행정구 included; nothing else is read. The snapshot already has
강원특별자치도 (51), 전북특별자치도 (52) and 대구광역시 군위군, which the previous source (a 2023-05
행정동코드 copy) lacked and this script patched by hand.

An institution is the government, not its label. The table is rebuilt on top of itself:

* A code in force sets name, 시도, 시군구 and region code. Aliases already in the table stay, and a
  name the institution had before becomes one, so older documents still resolve.
* A row whose code is no longer in force (광주광역시, 전라남도, 인천 중구·동구·서구) stays as it was:
  older documents were written by that government.
* 2026-07-01, 광주광역시 and 전라남도 → 전남광주통합특별시 (12). Its 시군구 are the same
  governments under new codes (46150 → 12150), so each keeps its institution code; the new 시도,
  its council and 교육청 are new rows. See ``MERGED_SIDO``.
* 2026-07-01, 인천: 서구 → 서해구 (28275) and 검단구 (28290); 중구 inland + 동구 → 제물포구 (28125);
  중구 영종 → 영종구 (28155). All four have new codes, here and in 조달청's 수요기관코드, and no
  old code goes on: four new rows, and 중구·동구·서구 stay as they were.

Codes that are not in the snapshot are not added. docs/real-data-run.md §9 has the reasoning.

Usage::

    uv run python scripts/build_institutions.py [scripts/data/stan_regin_cd_<date>.json]
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE.parent / "src/app/domain/data/institutions.csv"
SNAPSHOT = HERE / "data/stan_regin_cd_2026-09-27.json"
FIELDS = ["code", "name", "kind", "sido", "sigungu", "region_code", "executive_code", "aliases"]

# A 시도 made of others: its 시군구 are the old ones' governments, found by name.
MERGED_SIDO = {"12": ("29", "46")}  # 전남광주통합특별시 ← 광주광역시, 전라남도 (2026-07-01)
# 행정시 of 제주: no council of their own. 세종 has no 시군구 at all.
NO_COUNCIL = {"50110", "50130"}
# 특례시 (2022-01-13; 화성 2025-01-01) call themselves by that name in documents.
SPECIAL_CITIES = {
    "41110": "수원",
    "41280": "고양",
    "41460": "용인",
    "48120": "창원",
    "41590": "화성",
}
SEJONG = "36110"  # 시도 and 시 at once: one row, no 시군구
KIND_ORDER = {"local_gov": 0, "council": 1, "education_office": 2, "public_agency": 3}


def read_snapshot(path: Path) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """(시도 code → name, [(region code, 시도 name, 시군구 name)]) for autonomous units only."""
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    sido: dict[str, str] = {}
    sigungu: list[tuple[str, str, str]] = []
    for r in rows:
        region, words = r["region_cd"][:5], r["locatadd_nm"].split()
        if r["sgg_cd"] == "000" or region == SEJONG:
            sido[r["sido_cd"]] = words[0]
        if region == SEJONG:
            sigungu.append((region, words[0], ""))
        elif r["sgg_cd"] != "000" and len(words) == 2 and words[1].endswith(("시", "군", "구")):
            sigungu.append((region, words[0], words[1]))
        # Three words is a 행정구 ("경기도 화성시 만세구"): part of its 시, not a government.
    return sido, sigungu


def build(snapshot: Path, existing: list[dict[str, str]]) -> list[dict[str, str]]:
    sido, sigungu = read_snapshot(snapshot)
    old = {r["code"]: r for r in existing}
    rows: list[dict[str, str]] = []
    for sido_code, name in sido.items():
        region = f"{sido_code}000"
        if sido_code != SEJONG[:2]:
            rows.append(_row(f"LG-{region}", name, "local_gov", name, "", region))
            rows.append(
                _row(f"CN-{region}", f"{name}의회", "council", name, "", region, f"LG-{region}")
            )
        rows.append(_row(f"EO-{region}", f"{name}교육청", "education_office", name, "", region))
    for region, sido_name, sg in sigungu:
        if region == SEJONG:
            rows.append(_row(f"LG-{region}", sido_name, "local_gov", sido_name, "", region))
            council = f"{sido_name}의회"
            rows.append(
                _row(f"CN-{region}", council, "council", sido_name, "", region, f"LG-{region}")
            )
            continue
        code = _institution_code(region, sg, old)
        name = f"{sido_name} {sg}"
        special = SPECIAL_CITIES.get(code)
        aliases = [f"{special}특례시", f"{special}특례시청"] if special else []
        rows.append(_row(f"LG-{code}", name, "local_gov", sido_name, sg, region, "", aliases))
        if code not in NO_COUNCIL:
            council_aliases = [f"{special}특례시의회"] if special else []
            rows.append(
                _row(
                    f"CN-{code}",
                    f"{name}의회",
                    "council",
                    sido_name,
                    sg,
                    region,
                    f"LG-{code}",
                    council_aliases,
                )
            )
    return rows


def _institution_code(region: str, sigungu: str, old: dict[str, dict[str, str]]) -> str:
    predecessors = MERGED_SIDO.get(region[:2])
    if predecessors is None:
        return region
    found = [
        r["code"][3:]
        for r in old.values()
        if r["kind"] == "local_gov" and r["sigungu"] == sigungu and r["code"][3:5] in predecessors
    ]
    if len(found) != 1:
        sys.exit(f"{region} {sigungu}: {found or 'no'} predecessor in {predecessors}")
    return found[0]


def _row(
    code: str,
    name: str,
    kind: str,
    sido: str,
    sigungu: str,
    region: str,
    executive: str = "",
    aliases: list[str] | None = None,
) -> dict[str, str]:
    return {
        "code": code,
        "name": name,
        "kind": kind,
        "sido": sido,
        "sigungu": sigungu,
        "region_code": region,
        "executive_code": executive,
        "aliases": "|".join(aliases or []),
    }


def merge(existing: list[dict[str, str]], generated: list[dict[str, str]]) -> list[dict[str, str]]:
    """Generated rows set the facts; the table's aliases stay, and a former name joins them.
    Rows the snapshot no longer has (abolished governments, curated 공공기관) are kept."""
    by_code = {r["code"]: dict(r) for r in existing}
    for row in generated:
        before = by_code.get(row["code"])
        aliases = [a for a in before["aliases"].split("|") if a] if before else []
        if before and before["name"] != row["name"]:
            aliases.append(before["name"])
        aliases += [a for a in row["aliases"].split("|") if a]
        unique = [a for a in dict.fromkeys(aliases) if a != row["name"]]
        by_code[row["code"]] = row | {"aliases": "|".join(unique)}
    return sorted(
        by_code.values(),
        key=lambda r: (r["region_code"][:2], r["sigungu"] != "", r["region_code"],
                       KIND_ORDER[r["kind"]], r["code"]),
    )  # fmt: skip


def main() -> None:
    if len(sys.argv) > 2:
        sys.exit(__doc__)
    snapshot = Path(sys.argv[1]) if len(sys.argv) == 2 else SNAPSHOT
    existing = list(csv.DictReader(CSV_PATH.read_text(encoding="utf-8").splitlines()))
    rows = merge(existing, build(snapshot, existing))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    CSV_PATH.write_text(out.getvalue(), encoding="utf-8")
    print(f"{len(rows)} rows -> {CSV_PATH}")


if __name__ == "__main__":
    main()
