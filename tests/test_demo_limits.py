"""Public-demo guards: per-IP rate limits and the daily spend cap."""
from app.demo.limits import RateLimiter, SpendCap


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_per_minute_limit_then_recovers():
    clock = Clock()
    rl = RateLimiter(per_minute=3, per_day=100, clock=clock)
    assert [rl.check("1.2.3.4") for _ in range(3)] == [None, None, None]
    assert "a minute" in rl.check("1.2.3.4")
    assert rl.check("5.6.7.8") is None          # other visitors unaffected
    clock.t += 61
    assert rl.check("1.2.3.4") is None


def test_per_day_limit_resets_at_utc_midnight():
    clock = Clock(1_790_000_000.0)
    rl = RateLimiter(per_minute=100, per_day=2, clock=clock)
    assert rl.check("ip") is None and rl.check("ip") is None
    assert "daily limit" in rl.check("ip")
    clock.t += 86_400
    assert rl.check("ip") is None


def test_refused_attempts_dont_count():
    clock = Clock()
    rl = RateLimiter(per_minute=1, per_day=2, clock=clock)
    assert rl.check("ip") is None
    assert rl.check("ip")                        # refused by the minute limit
    clock.t += 61
    assert rl.check("ip") is None                # still has its second daily run


def test_spend_cap_trips_persists_and_resets(tmp_path):
    clock = Clock()
    ledger = tmp_path / "spend.json"
    cap = SpendCap(daily_usd=0.05, ledger_path=ledger, clock=clock)
    cap.record(0.03)
    assert not cap.exhausted()
    cap.record(0.025)
    assert cap.exhausted()
    # A restart reads the same day's spend back.
    assert SpendCap(daily_usd=0.05, ledger_path=ledger, clock=clock).exhausted()
    clock.t += 86_400
    assert not SpendCap(daily_usd=0.05, ledger_path=ledger, clock=clock).exhausted()
