"""Check real protection probes and bound their warm server execution time."""

import json
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text()
# The CLI emits progress followed by one complete, indented result object.
start = raw.index('\n{\n  "sizes":') + 1
report = json.loads(raw[start:])
Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
queries = {row["key"]: row["after"] for row in report["queries"]}
scope = report["supplemental_sizes"]
expected = {
    "reconciliation_protected_opportunities": scope["reconciliation_protected_opportunities"],
    "reconciliation_strict_opportunities": scope["reconciliation_strict_opportunities"],
    "reconciliation_strict_target_customer": 0,
    "reconciliation_strict_target_invalid_core": 1,
    "customer_anchor_document_protection_hit": 1,
    "customer_anchor_document_protection_miss": 0,
}
for key, rows in expected.items():
    actual = queries[key]
    limit_ms = 2000 if key.endswith("_opportunities") else 100
    if actual["rows"] != rows or actual["ms"] > limit_ms:
        raise SystemExit(f"{key}: {actual}; expected {rows} rows within {limit_ms}ms")
    print(f"{key}: {actual['rows']} rows, {actual['ms']}ms (limit {limit_ms}ms)")
