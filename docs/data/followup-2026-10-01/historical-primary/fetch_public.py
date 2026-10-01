"""Archive individual allowed public URLs and metadata; no crawl or retries."""
import datetime
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def fetch(key, url):
    meta = {"url": url, "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read()
            suffix = (
                ".pdf" if content.startswith(b"%PDF")
                else ".jpg" if content.startswith(b"\xff\xd8\xff")
                else ".png" if content.startswith(b"\x89PNG")
                else ".html"
            )
            filename = key + suffix
            (ROOT / filename).write_bytes(content)
            safe_headers = {k: v for k, v in response.headers.items() if k.lower() != "set-cookie"}
            meta.update(status=response.status, resolved_url=response.url, headers=safe_headers,
                        omitted_response_headers=["Set-Cookie"] if response.headers.get("Set-Cookie") else [],
                        file=filename, sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
            print(key, response.status, len(content), flush=True)
    except Exception as error:
        meta["error"] = str(error)
        print(key, str(error), flush=True)
    (ROOT / (key + ".meta.json")).write_text(json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    fetch(sys.argv[1], sys.argv[2])
