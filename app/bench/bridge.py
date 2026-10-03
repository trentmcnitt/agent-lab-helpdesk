"""Follows a running request-queue server's /api/stream and sends every run to the bench,
through client.py. For the operator server (live Slack), which needs no change to use it:

BENCH_URL=http://127.0.0.1:8790 uv run python -m app.bench.bridge --source http://127.0.0.1:8741

The public demo streams per visitor (a cookie picks the sandbox); it sends to the bench
itself when opened through the bench's shell, so it doesn't need this."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from . import client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="http://127.0.0.1:8741")
    ap.add_argument("--session", default="operator")
    ap.add_argument("--cookie", help="e.g. demo_sid=... to follow one public-demo sandbox")
    a = ap.parse_args()
    if not client.enabled():
        sys.exit("set BENCH_URL, e.g. http://127.0.0.1:8790")
    client.register()
    url, backoff = a.source.rstrip("/") + "/api/stream", 1.0
    while True:
        try:
            headers = {"Accept": "text/event-stream", **({"Cookie": a.cookie} if a.cookie else {})}
            print(f"bridging {url} -> {client.BASE} as session {a.session}", flush=True)
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                for raw in r:
                    backoff = 1.0
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        try:
                            client.send(a.session, json.loads(line[5:]))
                        except ValueError:
                            pass
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"source stream lost ({e.__class__.__name__}); retrying in {backoff:.0f}s", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
