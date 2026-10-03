"""Turns the recorded demo runs (demo/replays/*.json) into self-contained bench recordings:
a header with this app's map and story, then the run's bench events. Any bench can replay
them, including a static export, with nothing registered.

uv run python -m app.bench.recordings            -> demo/bench-recordings/*.recording.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import config
from . import client
from .adapter import convert_replay

LABEL = {"approved": "approved", "denied": "denied", None: "run"}


def build(replays: Path, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    header = {"v": "bench-recording/0", "topology": client.topology(), "story": client.story()}
    written = []
    for p in sorted(replays.glob("*.json")):
        rec = json.loads(p.read_text())
        for tail in (("approved", "denied") if rec.get("paused") else (None,)):
            events = convert_replay(p, tail, session_id="recording")
            name = f"{p.stem}-{LABEL[tail]}.recording.jsonl" if tail else f"{p.stem}.recording.jsonl"
            (out / name).write_text("".join(json.dumps(r) + "\n" for r in [header] + events))
            written.append(out / name)
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replays", default=str(config.REPO_ROOT / "demo/replays"))
    ap.add_argument("--out", default=str(config.REPO_ROOT / "demo/bench-recordings"))
    a = ap.parse_args()
    print(f"wrote {len(build(Path(a.replays), Path(a.out)))} recordings to {a.out}")


if __name__ == "__main__":
    main()
