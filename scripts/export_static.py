"""Exports the public demo's replay mode as a static site: the same page, with
static-adapter.js answering its /api/* calls in the browser from the recorded
runs. No server, no model calls; every run on the page is a recording.

    uv run scripts/export_static.py                  # -> dist/agentlabs/
    uv run scripts/export_static.py --out some/dir

The output uses relative paths only, so it works under any subpath. Served at
a URL with a trailing slash (/agentlabs/) it needs nothing else. A host that
serves it without the slash (/agentlabs) needs --base /agentlabs/, so the
scripts and data.json resolve under the subpath.

    uv run scripts/export_static.py --base /agentlabs/   # what trentmcnitt.com uses"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import demo_server  # noqa: E402  (scenario list and labels, shared with the hosted demo)
from app import config  # noqa: E402


def build_data() -> dict:
    scenarios, replays = [], {}
    for sid, req in demo_server.REQUESTS.items():
        rec = demo_server.REPLAYS.by_id.get(sid)
        if rec is None:
            continue  # a scenario with no recording can't run on a static page
        preview = req["message"].strip().replace("\n", " ")
        scenarios.append({"id": sid, "preview": preview[:97] + "..." if len(preview) > 100 else preview,
                          "expected_category": req["expected_category"],
                          "manual_estimate": config.MANUAL_ESTIMATE.get(req["expected_category"], "")})
        replays[sid] = rec
    models = {r.get("model") for r in replays.values()}
    return {"model": models.pop() if len(models) == 1 else "claude-sonnet-5", "scenarios": scenarios, "replays": replays}


def bench_header(replays: dict) -> dict:
    """The map and story the replays' runs carried, read from the bench recordings' headers. They
    must come from the same regeneration as the replays (regen_id), with one map for all of them."""
    heads = {}
    for p in sorted((ROOT / "demo" / "bench-recordings").glob("*.recording.jsonl")):
        heads[p.name.split(".")[0]] = json.loads(p.read_text().split("\n", 1)[0])
    for sid, rec in replays.items():
        names = [sid] if not rec.get("paused") else [f"{sid}-approved", f"{sid}-denied"]
        if not rec.get("regen_id") or any(heads.get(n, {}).get("regen_id") != rec["regen_id"] for n in names):
            raise SystemExit(f"{sid}: the replay and its bench recordings aren't from one regeneration: "
                             "run scripts/regen_demo.py")
    if len({json.dumps(h["topology"], sort_keys=True) for h in heads.values()}) != 1:
        raise SystemExit("the bench recordings carry different maps: run scripts/regen_demo.py")
    head = next(iter(heads.values()))
    return {"topology": head["topology"], "story": head["story"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist" / "agentlabs"))
    parser.add_argument("--base", default=None,
                        help="emit <base href=...> (e.g. /agentlabs/) for hosts that serve the page without a trailing slash")
    args = parser.parse_args()
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    page = (ROOT / "ui" / "index.html").read_text()
    marker = "<script>"
    assert page.count(marker) == 1, "expected exactly one inline <script> in ui/index.html"
    page = page.replace(marker, '<script src="replay-engine.js"></script>\n'
                                '<script src="static-adapter.js"></script>\n' + marker)
    if args.base:
        base = args.base if args.base.endswith("/") else args.base + "/"
        assert page.count("<head>") == 1, "expected exactly one <head> in ui/index.html"
        page = page.replace("<head>", f'<head>\n<base href="{base}">', 1)
    (out / "index.html").write_text(page)
    for name in ("replay-engine.js", "static-adapter.js"):
        shutil.copy(ROOT / "static" / name, out / name)
    # Inside the Agent Lab Bench shell, the page drives the bench beside it (SPEC 3a): each replay
    # carries its run's bench events, and the page hands the bench the map and story they were
    # recorded with, read from the recordings' own headers (scripts/regen_demo.py wrote both).
    data = build_data()
    data["bench"] = bench_header(data["replays"])
    (out / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    print(f"{out}: {len(data['scenarios'])} scenarios, "
          f"{sum(f.stat().st_size for f in out.iterdir()) // 1024} KB")


if __name__ == "__main__":
    main()
