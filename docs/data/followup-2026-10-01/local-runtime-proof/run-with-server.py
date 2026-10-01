#!/usr/bin/env python3
"""Start the local model and a client in the same exec network namespace."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

BASE = Path('/tmp/procurement-local-llm')
client = sys.argv[1:]
if client and client[0] == '--':
    client = client[1:]
if not client:
    raise SystemExit('Usage: python run-with-server.py -- <client command> [args...]')
start = time.perf_counter()
server = subprocess.Popen([str(BASE / 'start-server.sh')])
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(60):
        if server.poll() is not None:
            raise RuntimeError(f'server exited {server.returncode}; see {BASE / "server.log"}')
        try:
            with opener.open('http://127.0.0.1:18080/health', timeout=1) as response:
                health = json.load(response)
            if health.get('status') == 'ok':
                break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError(f'server health timeout; see {BASE / "server.log"}')
    print(json.dumps({'server_pid': server.pid, 'health': health,
                      'startup_seconds': round(time.perf_counter() - start, 3),
                      'endpoint': 'http://127.0.0.1:18080/v1/chat/completions'}, ensure_ascii=False), flush=True)
    result = subprocess.run(client, check=False)
    raise SystemExit(result.returncode)
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
