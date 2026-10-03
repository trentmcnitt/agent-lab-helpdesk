"""The policy decides, not the model. These force the model's decision with a
stub and check the code-level gate holds anyway -- no API calls, deterministic."""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app import config
from app.graph import ClassifyResult, DraftAnswer, ProposedAction, build_graph
from conftest import FakeLLM


APPROVER = {"id": "U_APPROVER", "name": "Pat Approver", "role": "admin", "via": "slack"}


def _state(role="viewer", auto_approve=True, message="please do the thing", requester_id="U_REQUESTER"):
    return {
        "run_id": "t", "request_id": "t", "requester_name": "Test User", "requester_role": role,
        "requester_id": requester_id,
        "channel": "helpdesk-requests", "thread_ts": "1", "message": message, "auto_approve": auto_approve,
    }


def _run(bus, index, board, llm, state):
    graph = build_graph(bus, index, board, checkpointer=InMemorySaver(), llm=llm)
    cfg = {"configurable": {"thread_id": "t"}}
    return graph, cfg, graph.invoke(state, config=cfg)


def _needs_write(action_type, target_tier=None, confidence=0.95):
    return FakeLLM({
        ClassifyResult: ClassifyResult(category="needs_write", rationale="stub", confidence=confidence),
        ProposedAction: ProposedAction(action_type=action_type, title="stub action", description="stub", target_tier=target_tier,
                                       system_as_written="the thing" if target_tier else None),
    })


@pytest.mark.parametrize("action_type", sorted(config.FORBIDDEN_ACTION_TYPES))
@pytest.mark.parametrize("role", config.ROLES)
def test_forbidden_action_never_executes_even_with_auto_approve(bus, index, board, action_type, role):
    _, _, result = _run(bus, index, board, _needs_write(action_type), _state(role=role, auto_approve=True))
    assert result["final_outcome"] == "escalated"
    assert result["permission_verdict"]["forbidden"] is True
    assert "__interrupt__" not in result  # never even reached the approval gate
    assert board.list_tickets() == []


def test_unknown_action_fails_closed(bus, index, board):
    _, _, result = _run(bus, index, board, _needs_write("drop_database"), _state(auto_approve=True))
    assert result["final_outcome"] == "escalated"
    assert board.list_tickets() == []


def test_low_confidence_escalates_before_any_write(bus, index, board):
    # No ProposedAction in the stub: reaching propose_action would raise.
    llm = FakeLLM({ClassifyResult: ClassifyResult(category="needs_write", rationale="stub", confidence=config.CONFIDENCE_THRESHOLD - 0.1)})
    _, _, result = _run(bus, index, board, llm, _state(auto_approve=True))
    assert result["category"] == "escalate" and result["original_category"] == "needs_write"
    assert result["final_outcome"] == "escalated"
    assert board.list_tickets() == []


def _digest_shown(result):
    return result["__interrupt__"][0].value["action_digest"]


def test_allowed_write_pauses_for_a_human_then_executes(bus, index, board):
    graph, cfg, result = _run(bus, index, board, _needs_write("create_ticket"), _state(auto_approve=False))
    assert "__interrupt__" in result and board.list_tickets() == []
    digest = _digest_shown(result)
    result = graph.invoke(Command(resume={"approved": True, "approver": APPROVER, "action_digest": digest}), config=cfg)
    assert result["final_outcome"] == "executed"
    assert result["approval"]["action_digest"] == digest and result["approval"]["by"] == "Pat Approver"
    tickets = board.list_tickets()
    assert len(tickets) == 1 and "Pat Approver" in tickets[0]["description"] and digest[:12] in tickets[0]["description"]


@pytest.mark.parametrize("presented", ["0" * 64, None])
def test_approval_for_a_different_action_executes_nothing(bus, index, board, presented):
    graph, cfg, _ = _run(bus, index, board, _needs_write("create_ticket"), _state(auto_approve=False))
    resume = {"approved": True, "approver": APPROVER}
    if presented:
        resume["action_digest"] = presented
    result = graph.invoke(Command(resume=resume), config=cfg)
    assert result["final_outcome"] == "escalated"
    assert result["approval"]["decision"] == "denied"
    assert board.list_tickets() == []


def test_denied_write_writes_nothing(bus, index, board):
    graph, cfg, _ = _run(bus, index, board, _needs_write("create_ticket", target_tier="viewer"), _state(auto_approve=False))
    result = graph.invoke(Command(resume={"approved": False, "approver": APPROVER}), config=cfg)
    assert result["final_outcome"] == "escalated"
    assert board.list_tickets() == []


@pytest.mark.parametrize("broken", ["classify", "draft_answer", "propose_action"])
def test_unparseable_model_output_fails_closed(bus, index, board, broken):
    # Seen for real once in 75 k=3 eval runs (draft_answer); it used to crash the run.
    category = "answerable" if broken == "draft_answer" else "needs_write"
    llm = FakeLLM({
        ClassifyResult: None if broken == "classify" else ClassifyResult(category=category, rationale="stub", confidence=0.95),
        DraftAnswer: None,
        ProposedAction: None,
    })
    _, _, result = _run(bus, index, board, llm, _state(auto_approve=True))
    assert result["final_outcome"] == "escalated"
    assert board.list_tickets() == []


def test_answer_citing_unretrieved_section_is_not_sent(bus, index, board):
    llm = FakeLLM({
        ClassifyResult: ClassifyResult(category="answerable", rationale="stub", confidence=0.95),
        DraftAnswer: DraftAnswer(answer="Just restart it.", cited_chunk_ids=["sec-999"]),
    })
    _, _, result = _run(bus, index, board, llm, _state(message="how do I reset my password"))
    assert result["grounded"] is False
    assert result["final_outcome"] == "escalated"


def test_requester_cannot_approve_their_own_request(bus, index, board):
    graph, cfg, paused = _run(bus, index, board, _needs_write("create_ticket", target_tier="admin"),
                              _state(auto_approve=False, requester_id="U_SELF"))
    me = {"id": "U_SELF", "name": "Self Approver", "role": "admin", "via": "slack"}
    result = graph.invoke(Command(resume={"approved": True, "approver": me, "action_digest": _digest_shown(paused)}), config=cfg)
    assert result["final_outcome"] == "escalated" and "own requests" in result["handoff_reason"]
    assert board.list_tickets() == []


@pytest.mark.parametrize("role", [None, "viewer", "operator"])
def test_only_approver_roles_can_approve(bus, index, board, role):
    graph, cfg, paused = _run(bus, index, board, _needs_write("create_ticket"), _state(auto_approve=False))
    who = {"id": "U_OTHER", "name": "Someone", "role": role, "via": "slack"}
    result = graph.invoke(Command(resume={"approved": True, "approver": who, "action_digest": _digest_shown(paused)}), config=cfg)
    assert result["final_outcome"] == "escalated" and board.list_tickets() == []


def test_digest_from_another_run_does_not_carry_over(bus, index, board):
    # Same action, same fields -- but a different run. Its approval must not apply here.
    _, _, other = _run(bus, index, board, _needs_write("create_ticket"), {**_state(auto_approve=False), "run_id": "other"})
    graph, cfg, _ = _run(bus, index, board, _needs_write("create_ticket"), _state(auto_approve=False))
    result = graph.invoke(Command(resume={"approved": True, "approver": APPROVER, "action_digest": _digest_shown(other)}), config=cfg)
    assert result["final_outcome"] == "escalated" and board.list_tickets() == []


def test_disguised_forbidden_action_is_refused(bus, index, board):
    # The model files an MFA-disable as an ordinary ticket; the policy reads what it says.
    llm = FakeLLM({
        ClassifyResult: ClassifyResult(category="needs_write", rationale="stub", confidence=0.95),
        ProposedAction: ProposedAction(action_type="create_ticket", title="Disable MFA for jdoe",
                                       description="User lost phone; turn off MFA so they can sign in"),
    })
    _, _, result = _run(bus, index, board, llm, _state(auto_approve=True))
    assert result["permission_verdict"]["forbidden"] is True
    assert result["final_outcome"] == "escalated" and board.list_tickets() == []


def test_grounding_rejects_unrelated_text_citing_a_real_section(index):
    from app.graph import _grounding_ok
    hits = index.retrieve("disable MFA lost phone")
    ok, _, overlap = _grounding_ok("Bananas are yellow and grow in tropical climates.", [hits[0]["chunk_id"]], hits)
    assert not ok and overlap < 0.15


def test_direct_grant_is_refused_even_with_auto_approve(bus, index, board):
    # Handbook section 8: access is granted after an approved Data Access Request ticket, not from chat.
    _, _, result = _run(bus, index, board, _needs_write("grant_access", target_tier="viewer"), _state(auto_approve=True))
    assert result["final_outcome"] == "escalated" and "section 8" in result["handoff_reason"]
    assert board.list_tickets() == []


def test_access_request_opens_a_data_access_request_ticket(bus, index, board):
    graph, cfg, paused = _run(bus, index, board, _needs_write("create_ticket", target_tier="viewer"), _state(auto_approve=False))
    result = graph.invoke(Command(resume={"approved": True, "approver": APPROVER, "action_digest": _digest_shown(paused)}), config=cfg)
    [ticket] = board.list_tickets()
    assert ticket["status"] == "open" and "data-access-request" in ticket["labels"]
    assert "section 8" in result["final_response"]


def test_rationale_citations_are_checked_against_retrieval():
    from app.graph import _rationale_citations
    retrieved = [{"chunk_id": "sec-15", "text": "## 15. Expense\nexcept as described in Section 12"},
                 {"chunk_id": "sec-11", "text": "## 11. Escalation"}]
    cites, unseen = _rationale_citations("Needs a Waypoint ticket per Section 15/5; see also sec-12 and §11.", retrieved)
    assert cites == [5, 11, 12, 15] and unseen == [5]  # 12 is cross-referenced inside a retrieved section


def test_denial_says_who_denied_it(bus, index, board):
    graph, cfg, paused = _run(bus, index, board, _needs_write("create_ticket"), _state(auto_approve=False))
    result = graph.invoke(Command(resume={"approved": False, "approver": APPROVER, "action_digest": _digest_shown(paused)}), config=cfg)
    assert result["final_outcome"] == "escalated" and "denied" in result["final_response"]
    assert board.list_tickets() == []


def test_cost_counts_cache_reads_and_writes():
    from app import config
    # 1,000 uncached + 8,000 written + 8,000 read + 100 out on Sonnet 5 pricing
    got = config.cost_for("claude-sonnet-5", 1000, 100, cache_read=8000, cache_write=8000)
    p = config.PRICING_PER_TOKEN["claude-sonnet-5"]
    assert abs(got - (1000 * p["input"] + 8000 * p["input"] * 0.1 + 8000 * p["input"] * 1.25 + 100 * p["output"])) < 1e-12


def test_grounding_rejects_an_answer_that_cites_the_wrong_section(index):
    from app.graph import _grounding_ok
    # A printer answer (section 14's words) that cites the VPN section instead.
    printer = next(c for c in index.chunks if c.section.startswith("14."))
    vpn = next(c for c in index.chunks if c.section.startswith("4."))
    answer = " ".join(printer.text.split()[:60])
    hits = [{"chunk_id": vpn.chunk_id, "text": vpn.text}, {"chunk_id": printer.chunk_id, "text": printer.text}]
    ok, reason, _ = _grounding_ok(answer, [vpn.chunk_id], hits, index.chunks)
    assert not ok and printer.chunk_id in reason
    ok, _, _ = _grounding_ok(answer, [printer.chunk_id], hits, index.chunks)
    assert ok


def _access(system_as_written):
    return FakeLLM({
        ClassifyResult: ClassifyResult(category="needs_write", rationale="stub", confidence=0.95),
        ProposedAction: ProposedAction(action_type="create_ticket", title="Data Access Request: X", description="stub",
                                       target_system="Churn Dashboard", target_tier="viewer",
                                       system_as_written=system_as_written),
    })


@pytest.mark.parametrize("quote", [None, "Churn Dashboard"])  # missing, or not in the message
def test_access_request_for_an_unnamed_system_goes_to_a_human(bus, index, board, quote):
    _, _, result = _run(bus, index, board, _access(quote),
                        _state(auto_approve=True, message="can I get the same access as jake on the dashboard from standup?"))
    assert result["final_outcome"] == "escalated" and "which system" in result["handoff_reason"]
    assert board.list_tickets() == []


def test_access_request_for_a_named_system_proceeds(bus, index, board):
    _, _, result = _run(bus, index, board, _access("the Compass analytics dashboard"),
                        _state(auto_approve=True, message="need read access to the Compass analytics dashboard please"))
    assert result["final_outcome"] == "executed"
