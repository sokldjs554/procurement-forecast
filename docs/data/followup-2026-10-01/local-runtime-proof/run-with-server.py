#!/usr/bin/env python3
"""Verify and start the local model and client in the same exec network namespace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path("/tmp/procurement-local-llm")  # noqa: S108 -- Existing pinned, local runtime installation.
MODEL_PATH = BASE / "Qwen3-4B-Q4_K_M.gguf"
MODEL_SHA256 = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
RUNTIME_PATH = BASE / "runtime/llama-b11308/llama-server"
RUNTIME_REVISION = "feb9a3d6debb3a8544052b04c84fa1f445fd77f5"
START_SCRIPT = Path(__file__).resolve().with_name("start-server.sh")
HOST = "127.0.0.1"
PORT = 18080


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_runtime() -> dict[str, object]:
    model_hash = file_sha256(MODEL_PATH)
    if model_hash != MODEL_SHA256:
        raise RuntimeError("local model SHA256 does not match the frozen model")
    version = subprocess.run(  # noqa: S603 -- Fixed local runtime path; no shell.
        [str(RUNTIME_PATH), "--version"], capture_output=True, text=True, check=True, timeout=30
    )
    version_text = (version.stdout + version.stderr).strip()
    commit = re.search(r"\bcommit\s+([0-9a-f]{9,40})\b", version_text)
    if commit is None or not RUNTIME_REVISION.startswith(commit.group(1)):
        raise RuntimeError(f"local llama-server revision mismatch: {version_text}")
    return {
        "model_path": str(MODEL_PATH),
        "model_sha256": model_hash,
        "runtime_path": str(RUNTIME_PATH),
        "runtime_sha256": file_sha256(RUNTIME_PATH),
        "runtime_revision": RUNTIME_REVISION,
        "runtime_version": version_text,
        "start_script": str(START_SCRIPT),
        "start_script_sha256": file_sha256(START_SCRIPT),
        "wrapper_sha256": file_sha256(Path(__file__)),
        "pins_verified": True,
    }


def ensure_port_available() -> None:
    # A pre-existing listener must never supply health or completion responses.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, PORT))
        except OSError as exc:
            raise RuntimeError(f"local endpoint {HOST}:{PORT} is already in use") from exc


def ensure_alive(server: subprocess.Popen) -> None:
    if server.poll() is not None:
        raise RuntimeError(f"server exited {server.returncode}; see {BASE / 'server.log'}")


def main(client: list[str]) -> int:
    if client and client[0] == "--":
        client = client[1:]
    if not client:
        raise SystemExit("Usage: python run-with-server.py -- <client command> [args...]")
    proof = verify_runtime()
    ensure_port_available()
    start = time.perf_counter()
    server = subprocess.Popen(["/bin/bash", str(START_SCRIPT)])  # noqa: S603 -- Reviewed sibling script.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(60):
            ensure_alive(server)
            try:
                with opener.open(f"http://{HOST}:{PORT}/health", timeout=1) as response:
                    health = json.load(response)
                if isinstance(health, dict) and health.get("status") == "ok":
                    ensure_alive(server)
                    break
            except (OSError, urllib.error.URLError, ValueError):
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(f"server health timeout; see {BASE / 'server.log'}")
        proof.update(
            {
                "server_pid": server.pid,
                "health": health,
                "startup_seconds": round(time.perf_counter() - start, 3),
                "endpoint": f"http://{HOST}:{PORT}/v1/chat/completions",
            }
        )
        print(json.dumps(proof, ensure_ascii=False), flush=True)
        environment = os.environ.copy()
        environment["PROCUREMENT_LOCAL_MODEL_RUNTIME_PROOF"] = json.dumps(proof)
        ensure_alive(server)
        # The caller explicitly supplies the local command; arguments never pass through a shell.
        return subprocess.run(client, check=False, env=environment).returncode  # noqa: S603
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
