#!/usr/bin/env python3
"""Read-only offline follow-up report. Redirect stdout to a separate report file."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--horizon-days", type=int, default=540)
    parser.add_argument("--as-of", type=date.fromisoformat)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api" / "src"))
    from app.eval.longitudinal import evaluate_longitudinal

    try:
        report = evaluate_longitudinal(
            args.snapshot,
            args.observations,
            horizon_days=args.horizon_days,
            as_of=args.as_of,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
