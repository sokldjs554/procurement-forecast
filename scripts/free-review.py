#!/usr/bin/env python3
"""Start/check/stop the isolated, no-cloud-charge review stack using only host Python + Docker."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def compose(*args):
    # Explicit file and project: never inherit COMPOSE_FILE/PROJECT_NAME or a user's .env.
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / "scripts/review.env"),
            "--project-name",
            "procurement-forecast-review",
            "--file",
            str(ROOT / "docker-compose.review.yml"),
            *args,
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["start", "check", "stop", "status", "fingerprint"]
    )
    args = parser.parse_args()
    if not shutil.which("docker"):
        parser.exit(
            1,
            "Docker와 Docker Compose v2가 필요합니다. 설치 후 Docker를 실행해 주세요.\n",
        )
    if args.command == "start":
        compose("build", "api", "web")
        compose("up", "-d", "--wait", "--wait-timeout", "120", "db", "redis", "mailpit")
        # No concurrent cron/queue work while preparing/migrating the same local database.
        compose("stop", "gateway", "api", "worker", "web")
        compose("run", "--rm", "--no-deps", "-T", "api", "manage", "db", "upgrade")
        compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "api",
            "python",
            "/review/data.py",
            "prepare",
        )
        compose("up", "-d", "--wait", "--wait-timeout", "180", "api", "worker", "web")
        compose("up", "-d", "gateway")
        compose("exec", "-T", "api", "python", "/review/data.py", "check")
        for url in (
            "http://localhost:13000/login",
            "http://localhost:18000/readyz",
            "http://localhost:18025/api/v1/messages",
        ):
            for attempt in range(30):
                try:
                    with urllib.request.urlopen(url, timeout=3) as response:
                        response.read(1)
                    break
                except (urllib.error.URLError, TimeoutError):
                    if attempt == 29:
                        raise SystemExit(f"호스트 접속 확인 실패: {url}")
                    time.sleep(1)
        print(
            "\n실행 준비 완료: http://localhost:13000  |  메일함: http://localhost:18025"
        )
        print(
            "합성 자료 · 규칙 기반 추출 · 모의 결제 · 메일은 로컬 메일함에만 저장됩니다."
        )
    elif args.command in ("check", "fingerprint"):
        compose("exec", "-T", "api", "python", "/review/data.py", args.command)
    elif args.command == "stop":
        # Intentionally no -v: keep DB, raw documents, queue and captured mail.
        compose("down")
        print("종료했습니다. DB·원문·메일·Redis 데이터는 다음 실행까지 보존됩니다.")
    else:
        compose("ps")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(
            "명령이 실패했습니다. 데이터 볼륨은 삭제하지 않았습니다.", file=sys.stderr
        )
        sys.exit(exc.returncode)
