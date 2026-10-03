"""The public demo app: isolation between visitors, no operator surface, the
rate limit and the spend cap falling back to recorded replays, and replay
approvals honoring the digest. Live runs use a stubbed model."""
import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.graph import ClassifyResult, ProposedAction
from conftest import FakeLLM


def _load(monkeypatch, tmp_path, **env):
    base = {"DEMO_DATA_DIR": str(tmp_path), "DEMO_SECURE_COOKIE": "0", "DEMO_MODE": "replay",
            "DEMO_RATE_PER_MINUTE": "3", "DEMO_RATE_PER_DAY": "20", "DEMO_DAILY_USD": "2.00"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    import demo_server
    return importlib.reload(demo_server)


def _stub_live(ds, monkeypatch, cost=0.01):
    """Live mode with a stubbed model: every run proposes a ticket and 'costs' `cost`."""
    from app import config
    from app.demo.engine import LiveRunner
    from app.retrieval import HandbookIndex

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    llm = FakeLLM({ClassifyResult: ClassifyResult(category="needs_write", rationale="stub", confidence=0.95),
                   ProposedAction: ProposedAction(action_type="create_ticket", title="stub ticket", description="stub")})
    runner = LiveRunner(HandbookIndex(mode="bm25"), on_spend=ds.SPEND.record, llm=llm)
    orig = runner._bus

    def bus_with_cost(box, run_id, recorder):
        bus = orig(box, run_id, recorder)
        bus.publish(node="classify", event_type="llm_call", cost_usd=cost)  # stand-in for the real call's cost
        return bus

    runner._bus = bus_with_cost
    ds._live_runner = runner
    return runner


def _box(ds, client):
    return ds.SANDBOXES.get(client.cookies.get("demo_sid"))


def _wait(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _client(ds):
    c = TestClient(ds.app)
    c.get("/")  # sets the session cookie
    return c


def test_no_operator_surface(monkeypatch, tmp_path):
    ds = _load(monkeypatch, tmp_path)
    paths = {r.path for r in ds.app.routes}
    assert "/api/requests/custom" in paths and "/docs" not in paths and "/openapi.json" not in paths
    import inspect
    assert "OPERATOR_TOKEN" not in inspect.getsource(ds)
    # Free text is off by default.
    assert _client(ds).post("/api/requests/custom", json={"message": "hi"}).status_code == 404


def test_replay_runs_with_no_model_and_approval_honors_the_digest(monkeypatch, tmp_path):
    ds = _load(monkeypatch, tmp_path)
    rec = json.loads((ds.REPLAY_DIR / "req-012.json").read_text())
    c = _client(ds)
    assert c.get("/api/status").json()["mode"] == "replay"

    r = c.post("/api/requests/seed/req-012").json()
    assert r["mode"] == "replay"
    box = _box(ds, c)
    assert _wait(lambda: r["run_id"] in box.runs)          # paused at the recorded approval card
    assert c.post(f"/api/approve/{r['run_id']}", json={"approved": True, "action_digest": "wrong"}).status_code == 200
    assert _wait(lambda: len(c.get(f"/api/thread/helpdesk-requests/{r['thread_ts']}").json()) == 2)
    assert c.get("/api/board").json() == []                  # wrong digest -> the denied ending, nothing opened

    r = c.post("/api/requests/seed/req-012").json()
    assert _wait(lambda: r["run_id"] in box.runs)
    c.post(f"/api/approve/{r['run_id']}", json={"approved": True, "action_digest": rec["action_digest"]})
    assert _wait(lambda: len(c.get("/api/board").json()) == 1)
    assert ds.SPEND.spent_today() == 0.0                     # replays never spend


def test_visitors_are_isolated(monkeypatch, tmp_path):
    ds = _load(monkeypatch, tmp_path)
    rec = json.loads((ds.REPLAY_DIR / "req-012.json").read_text())
    alice, bob = _client(ds), _client(ds)
    r = alice.post("/api/requests/seed/req-012").json()
    assert _wait(lambda: r["run_id"] in _box(ds, alice).runs)
    # Bob can't see Alice's thread or approve her run.
    assert bob.get(f"/api/thread/helpdesk-requests/{r['thread_ts']}").json() == []
    assert bob.post(f"/api/approve/{r['run_id']}", json={"approved": True,
                                                        "action_digest": rec["action_digest"]}).status_code == 404
    alice.post(f"/api/approve/{r['run_id']}", json={"approved": True, "action_digest": rec["action_digest"]})
    assert _wait(lambda: len(alice.get("/api/board").json()) == 1)
    assert bob.get("/api/board").json() == []


def test_live_run_then_rate_limit_falls_back_to_replay(monkeypatch, tmp_path):
    ds = _load(monkeypatch, tmp_path, DEMO_MODE="live", DEMO_RATE_PER_MINUTE="1")
    _stub_live(ds, monkeypatch)
    c = _client(ds)
    first = c.post("/api/requests/seed/req-012").json()
    assert first["mode"] == "live"
    assert _wait(lambda: first["run_id"] in _box(ds, c).runs)   # the live run paused for approval
    second = c.post("/api/requests/seed/req-012").json()
    assert second["mode"] == "replay" and "a minute" in second["mode_reason"]


def test_spend_cap_flips_the_demo_to_replay(monkeypatch, tmp_path):
    ds = _load(monkeypatch, tmp_path, DEMO_MODE="live", DEMO_DAILY_USD="0.015", DEMO_RATE_PER_MINUTE="10")
    _stub_live(ds, monkeypatch, cost=0.01)
    c = _client(ds)
    assert c.get("/api/status").json()["mode"] == "live"
    r = c.post("/api/requests/seed/req-019").json()
    assert r["mode"] == "live"
    assert _wait(lambda: r["run_id"] in _box(ds, c).runs)
    r = c.post("/api/requests/seed/req-019").json()           # 0.01 spent, still under 0.015
    assert r["mode"] == "live"
    assert _wait(lambda: ds.SPEND.exhausted())
    s = c.get("/api/status").json()
    assert s["mode"] == "replay" and "budget" in s["mode_reason"]
    r = c.post("/api/requests/seed/req-019").json()
    assert r["mode"] == "replay"


@pytest.mark.parametrize("path", ["/api/requests/seed/req-999", "/api/requests/seed/../../etc"])
def test_only_listed_scenarios_run(monkeypatch, tmp_path, path):
    ds = _load(monkeypatch, tmp_path)
    assert _client(ds).post(path).status_code == 404


def test_replay_reply_names_the_ticket_on_the_visitors_board(monkeypatch, tmp_path):
    ds = _load(monkeypatch, tmp_path)
    rec = json.loads((ds.REPLAY_DIR / "req-011.json").read_text())
    c = _client(ds)
    r = c.post("/api/requests/seed/req-011").json()
    assert _wait(lambda: r["run_id"] in _box(ds, c).runs)
    c.post(f"/api/approve/{r['run_id']}", json={"approved": True, "action_digest": rec["action_digest"]})
    assert _wait(lambda: len(c.get(f"/api/thread/helpdesk-requests/{r['thread_ts']}").json()) == 2)
    [ticket] = c.get("/api/board").json()
    reply = c.get(f"/api/thread/helpdesk-requests/{r['thread_ts']}").json()[-1]["text"]
    assert ticket["id"] in reply and rec["tails"]["approved"]["tickets"][0]["id"] not in reply
