"""This app's adapter for the bench: its event bus -> bench events (native, bench/0).

Part of the app's bench kit (app/bench/): the adapter, the map (topology.json), the story
(story.js) and the client that registers them and sends events. The bench itself holds
nothing about this app. The mapping is in app/bench/README.md.

Source events are {run_id, node, event_type, data, ts}: the JSONL run logs, the SSE stream
(which adds `_meta` phase events), and the recorded replays.
The source has no step_finished, so the adapter closes a node's step when the next node's
first event arrives, or when the run ends, and marks that latency as inferred.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

V = "bench/0"
COST_BASIS = "request-queue price table (app/config.py)"


class AgentLabsAdapter:
    """Stateful: feed one run's events in order, then call finish(). One instance per run."""

    def __init__(self, session_id: str | None = None, content_mode: str = "redacted"):
        self.session_id = session_id
        self.content_mode = content_mode
        self.run_id: str | None = None
        self.seq = 0
        self.started = False
        self.finished = False
        self.open_node: str | None = None
        self.open_step: str | None = None
        self.open_ts = 0.0
        self.open_last = 0.0
        self.step_counts: dict[str, int] = {}
        self.last_ts = 0.0
        self.saw_error = False

    # -- helpers -----------------------------------------------------------------------
    def _ev(self, node: str, event_type: str, ts: float, data: dict[str, Any] | None = None,
            step_id: str | None = None) -> dict[str, Any]:
        ev = {"v": V, "run_id": self.run_id, "seq": self.seq, "ts": ts, "node": node,
              "event_type": event_type, "content_mode": self.content_mode, "data": data or {}}
        if self.session_id:
            ev["session_id"] = self.session_id
        if step_id:
            ev["step_id"] = step_id
        self.seq += 1
        return ev

    def _close_step(self, ts: float, status: str = "ok") -> list[dict]:
        """Closes the open step at `ts`. For an inferred boundary, callers pass the step's own
        last event time: the source only publishes when work finishes (llm_call arrives after
        the model returns), so the gap before a node's first event is that node's work."""
        if self.open_node is None:
            return []
        out = [self._ev(self.open_node, "step_finished", ts,
                        {"status": status, "latency_ms": round(max(0.0, ts - self.open_ts) * 1000, 1),
                         "latency_inferred": True}, self.open_step)]
        self.open_node = self.open_step = None
        return out

    def _open_step(self, node: str, ts: float, data: dict | None = None) -> list[dict]:
        n = self.step_counts.get(node, 0) + 1
        self.step_counts[node] = n
        self.open_node, self.open_step, self.open_ts = node, f"{self.run_id}:{node}:{n}", ts
        self.open_last = ts
        return [self._ev(node, "step_started", ts, data, self.open_step)]

    def _start_run(self, ts: float, data: dict | None = None) -> list[dict]:
        self.started = True
        return [self._ev("_run", "run_started", ts, data or {})]

    # -- public ------------------------------------------------------------------------
    def feed(self, src: dict[str, Any]) -> list[dict[str, Any]]:
        if self.finished:
            return []
        self.run_id = self.run_id or src["run_id"]
        node, et, data, ts = src["node"], src["event_type"], dict(src.get("data") or {}), float(src["ts"])
        self.last_ts = max(self.last_ts, ts)
        out: list[dict] = []

        if node == "_meta":
            phase = data.get("phase")
            if phase == "started":
                if not self.started:
                    meta = {k: data[k] for k in ("origin",) if k in data}
                    out += self._start_run(ts, meta)
            elif phase == "done":
                out += self.finish(ts, status="error" if self.saw_error else "ok",
                                   output=data.get("final_response"))
            elif phase == "error":
                self.saw_error = True
                out += self._close_step(ts, "error")
                out.append(self._ev("_run", "error", ts, {"message": str(data.get("error", "run failed"))}))
                out += self.finish(ts, status="error")
            # awaiting_approval / rehydrated: gate_waiting / gate_resolved already carry these.
            return out

        if not self.started:
            first = {"input": data["message"]} if et == "node_enter" and "message" in data else {}
            out += self._start_run(ts, first)

        if node != self.open_node:
            # The previous step ends at its own last event; this node's work began then.
            boundary = self.open_last if self.open_node is not None else ts
            out += self._close_step(boundary)
            out += self._open_step(node, boundary, {"input": data["message"]} if et == "node_enter" and "message" in data else None)
            self.open_last = ts
            if et == "node_enter":
                return out
        else:
            self.open_last = ts
            if et == "node_enter":
                return out

        step = self.open_step
        if et == "retrieval_hits":
            hits = []
            for h in data.get("hits", []):
                hit = {"id": str(h.get("chunk_id", h.get("id", "?"))), "title": h.get("section", ""),
                       "score": h.get("score")}
                if hit["score"] is None:
                    del hit["score"]
                for k in ("bm25", "embed", "text"):
                    if k in h:
                        hit[k] = h[k]
                hits.append(hit)
            rest = {k: v for k, v in data.items() if k != "hits"}
            out.append(self._ev(node, "retrieval", ts, {"hits": hits, **rest}, step))
        elif et == "decision":
            # `branch` names the edge taken, matching the map's from_branch (Agent Spec branch_selected).
            if "category" in data:
                data.setdefault("branch", data["category"])
            elif "grounded" in data:
                data.setdefault("branch", "grounded" if data["grounded"] else "not_grounded")
            out.append(self._ev(node, "decision", ts, data, step))
        elif et == "llm_call":
            llm = {k: data[k] for k in ("model", "input_tokens", "output_tokens", "cache_read_tokens",
                                         "cache_write_tokens", "cost_usd", "sampling", "system", "messages",
                                         "output", "params") if k in data and data[k] is not None}
            llm.setdefault("model", "unknown")
            llm.setdefault("input_tokens", 0)
            llm.setdefault("output_tokens", 0)
            llm["provider"] = "anthropic"
            if "cost_usd" in llm:
                llm["cost_source"] = "estimated"
                llm["cost_basis"] = COST_BASIS
            out.append(self._ev(node, "llm_call", ts, llm, step))
        elif et == "approval_requested":
            gw = {"proposed": data.get("proposed_action"), "digest": data.get("action_digest")}
            out.append(self._ev(node, "gate_waiting", ts, {k: v for k, v in gw.items() if v is not None}, step))
        elif et == "approval_result":
            gr = {"approved": bool(data.get("approved")), "by": data.get("approved_by"),
                  "via": data.get("approver_via"), "digest_match": data.get("digest_match"),
                  "reason": data.get("reason"), "mode": data.get("mode")}
            out.append(self._ev(node, "gate_resolved", ts, {k: v for k, v in gr.items() if v is not None}, step))
        elif et == "model_output_error":
            out.append(self._ev(node, et, ts, data, step))
            out.append(self._ev(node, "error", ts, {"message": str(data.get("parsing_error", "model output did not parse")),
                                                    "type": "model_output_error", "retryable": True}, step))
        else:
            # permission_verdict, tool_call, respond, handoff, and anything added later.
            out.append(self._ev(node, et, ts, data, step))
        return out

    def finish(self, ts: float | None = None, status: str = "ok", output: Any = None) -> list[dict]:
        if self.finished or self.run_id is None:
            return []
        ts = self.last_ts if ts is None else ts
        out = self._close_step(ts)
        data: dict[str, Any] = {"status": status}
        if output is not None:
            data["output"] = output
        out.append(self._ev("_run", "run_finished", ts, data))
        self.finished = True
        return out


def convert(events: Iterable[dict], session_id: str | None = None, content_mode: str = "redacted",
            paused: bool = False) -> list[dict]:
    """One run's source events -> bench events. paused=True leaves the run open (it stopped at the gate)."""
    a = AgentLabsAdapter(session_id, content_mode)
    out: list[dict] = []
    for ev in events:
        out += a.feed(ev)
    if not paused:
        out += a.finish()
    return out


def convert_replay(path: Path, tail: str = "approved", session_id: str | None = "replay") -> list[dict]:
    """A demo/replays/*.json recording -> bench events, with the chosen tail (approved | denied)."""
    rec = json.loads(Path(path).read_text())
    src, final = list(rec["pre"]), rec.get("final")
    if rec.get("paused") and tail:
        src += rec["tails"][tail]["events"]
        final = rec["tails"][tail].get("final")
    # A recording has no _meta phases; its "final" is what the live run's `done` phase carried.
    a = AgentLabsAdapter(session_id, "redacted")
    out: list[dict] = []
    for ev in src:
        out += a.feed(ev)
    return out + a.finish(output=final)


def convert_jsonl(path: Path, session_id: str | None = "runs") -> list[dict]:
    lines = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    return convert(lines, session_id=session_id)
