"""Read-only HTTP checks for an already deployed production stack (stdlib only)."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def check(origin: str, path: str, expected: int, *, json_body: bool, timeout: int) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
        raise ValueError("Provide an HTTP(S) origin without embedded credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Provide the service origin without a path, query, or fragment")
    request = Request(  # noqa: S310 - origin scheme validated above
        origin.rstrip("/") + path, headers={"User-Agent": "procurement-render-smoke/1"}
    )
    try:
        response = urlopen(request, timeout=timeout)  # noqa: S310 - origin validated above
    except HTTPError as error:
        response = error
    with response:
        status = response.status
        content_type = response.headers.get("content-type", "")
        body = response.read(1_000_000)
    if status != expected:
        raise RuntimeError(f"{parsed.hostname}{path}: expected HTTP {expected}, received {status}")
    if json_body:
        if "application/json" not in content_type:
            raise RuntimeError(f"{path}: expected JSON, received another content type")
        payload = json.loads(body)
        if expected == 200 and payload.get("status") != "ok":
            raise RuntimeError(f"{path}: dependency status is not ok")
    elif path == "/login" and "text/html" not in content_type:
        raise RuntimeError("/login: expected the rendered frontend HTML")
    print(f"PASS {parsed.hostname}{path} HTTP {status}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", required=True, help="Public frontend origin")
    parser.add_argument("--api", help="API origin; verifies /healthz and /readyz")
    parser.add_argument("--worker", help="Worker origin when reachable from the invoking host")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()
    check(args.web, "/login", 200, json_body=False, timeout=args.timeout)
    check(args.web, "/api/me", 401, json_body=True, timeout=args.timeout)
    check(args.web, "/api/admin/overview", 401, json_body=True, timeout=args.timeout)
    if args.api:
        check(args.api, "/healthz", 200, json_body=True, timeout=args.timeout)
        check(args.api, "/readyz", 200, json_body=True, timeout=args.timeout)
    if args.worker:
        check(args.worker, "/healthz", 200, json_body=False, timeout=args.timeout)
    print(
        "Read-only smoke checks passed. Ingestion, authenticated flows, billing, and delivery still require verification."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, URLError, TimeoutError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from None
