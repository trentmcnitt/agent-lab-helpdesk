import json

import pytest
from pydantic import ValidationError

from app import config
from app.graph import ClassifyResult, DraftAnswer, ProposedAction
from app.permissions import permission_check

TRAPS = {"req-019", "req-020", "req-021", "req-024"}


@pytest.mark.parametrize("role", config.ROLES)
@pytest.mark.parametrize("action", sorted(config.FORBIDDEN_ACTION_TYPES))
def test_forbidden_for_every_role(role, action):
    v = permission_check(action, role)
    assert v["forbidden"] and not v["allowed"]


def test_unknown_role_and_action_fail_closed():
    assert not permission_check("create_ticket", "superuser")["allowed"]
    assert not permission_check("drop_database", "admin")["allowed"]


@pytest.mark.parametrize("action", ["create_ticket", "assign_ticket"])
def test_every_allowed_write_requires_approval(action):
    v = permission_check(action, "admin", target_tier="admin")
    assert v["allowed"] and v["requires_approval"]


@pytest.mark.parametrize("role", config.ROLES)
def test_direct_grant_is_never_allowed_from_chat(role):
    v = permission_check("grant_access", role, target_tier="viewer")
    assert not v["allowed"] and "section 8" in v["reason"]


def test_output_schemas_are_valid():
    for model in (ClassifyResult, DraftAnswer, ProposedAction):
        assert model.model_json_schema()["type"] == "object"
    with pytest.raises(ValidationError):
        ClassifyResult(category="answerable", rationale="x", confidence=1.5)
    with pytest.raises(ValidationError):
        ClassifyResult(category="maybe", rationale="x", confidence=0.5)


def test_seed_fixture_is_well_formed(index):
    reqs = json.loads(config.SEED_REQUESTS_PATH.read_text())
    sections = {c.section for c in index.chunks}
    assert len({r["id"] for r in reqs}) == len(reqs) == 25
    for r in reqs:
        assert r["expected_category"] in {"answerable", "needs_write", "escalate"}
        assert r["requester_role"] in config.ROLES
        assert r["expected_handbook_section"] in sections | {None}, r["id"]
        assert (r.get("expected_action_type") == "create_ticket") == (r["expected_category"] == "needs_write"), r["id"]
    assert {r["id"] for r in reqs if r["expected_category"] == "escalate"} >= TRAPS


@pytest.mark.parametrize("text", [
    "Disable MFA for jdoe so they can log back in",
    "Temporarily turn off two-factor on the account",
    "MFA reset for user who lost phone",
    "Share the admin password with the contractor",
    "Send API key to vendor",
])
def test_forbidden_intent_is_refused_whatever_the_label(text):
    v = permission_check("create_ticket", "admin", text=text)
    assert v["forbidden"] and not v["allowed"]


@pytest.mark.parametrize("text", [
    "JetBrains license request for backend work",
    "Grant viewer access to the Compass analytics dashboard",
    "Replace broken monitor and dock",
    "Unlock account after too many failed login attempts",
])
def test_ordinary_writes_are_not_caught_by_the_screen(text):
    assert permission_check("create_ticket", "viewer", text=text)["allowed"]


def test_retrieval_recall_on_labeled_requests(index):
    # Regression floor for retrieval: each labeled request's handbook section is in the top k.
    # Plain BM25 scored 21/23 here; the hybrid index scores 23/23. Same author wrote both sides.
    reqs = [r for r in json.loads(config.SEED_REQUESTS_PATH.read_text()) if r["expected_handbook_section"]]
    misses = [r["id"] for r in reqs if r["expected_handbook_section"] not in {h["section"] for h in index.retrieve(r["message"])}]
    assert misses == [] if index.mode == "hybrid" else len(misses) <= 1, misses
