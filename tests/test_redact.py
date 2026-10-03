"""Trace redaction: secrets and contact details are masked in what the event
bus records, and TRACE_CONTENT=full turns it off for local demos."""
from app import config
from app.events import EventBus
from app.redact import redact_text

LEAKY = ("my password is Hunter2!! and the key is sk-ant-api03-AbCdEfGhIjKlMnOpQrSt, "  # gitleaks:allow (fake)
         "slack xoxb-1234567890-abcdefghij, email jo@northwire.example, call 414-555-0142")


def test_secrets_and_contacts_are_masked():
    out = redact_text(LEAKY)
    for secret in ("Hunter2", "sk-ant-api03", "xoxb-1234567890", "jo@northwire.example", "414-555-0142"):  # gitleaks:allow (fake)
        assert secret not in out
    assert "password is [redacted]" in out


def test_ordinary_trace_content_is_untouched():
    text = "action sha256:bcd8b3701af2 ts 1790559960.303219 grant viewer on Compass, see sec-8"
    assert redact_text(text) == text


def test_event_bus_masks_what_it_records(tmp_path, monkeypatch):
    seen = []
    bus = EventBus("r", tmp_path / "r.jsonl")
    bus.subscribe(seen.append)
    bus.publish(node="ingest", event_type="node_enter", message=LEAKY, nested={"hits": [LEAKY]})
    logged = (tmp_path / "r.jsonl").read_text()
    assert "Hunter2" not in logged and "Hunter2" not in str(seen[0].data)

    monkeypatch.setattr(config, "TRACE_CONTENT", "full")
    bus.publish(node="ingest", event_type="node_enter", message=LEAKY)
    assert "Hunter2" in (tmp_path / "r.jsonl").read_text().splitlines()[-1]
