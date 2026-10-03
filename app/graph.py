"""The graph: a deterministic skeleton with an LLM at exactly three fuzzy
points (classify, draft_answer, propose_action). Every other node is plain
code. 'The model proposes, the policy decides' -- permission_check never
calls an LLM, and low-confidence classifications are overridden to escalate
regardless of what the model picked. See ARCHITECTURE.md for the full
rationale and the failure mode (the documented rip-out) this is built to
avoid."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from . import config, permissions
from .board_adapter import BoardAdapter
from .events import EventBus
from .langfuse_sink import get_langfuse_handler
from .retrieval import STOPWORDS as _STOPWORDS, HandbookIndex, _tokenize


class ClassifyResult(BaseModel):
    category: Literal["answerable", "needs_write", "escalate"]
    rationale: str = Field(
        description=(
            "One or two sentences a human could audit this decision by. Phrase it as the "
            "decision itself, not a deferral -- e.g. for needs_write, 'This is a data-access "
            "request, which needs a Data Access Request ticket' (the agent will propose and, once "
            "approved, execute the write itself), not 'this should be routed to X rather than judged here' "
            "(that phrasing describes escalate, where a human takes over -- don't use it for "
            "needs_write, which the agent completes itself once approved)."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0)


class DraftAnswer(BaseModel):
    answer: str
    cited_chunk_ids: list[str] = Field(description="Handbook chunk ids (e.g. 'sec-3') this answer relies on.")


class ProposedAction(BaseModel):
    action_type: str = Field(
        description=(
            "Use 'create_ticket' for any ordinary work item -- VPN/hardware/software renewals or "
            "requests, standard follow-ups -- even if the handbook's own instructions describe "
            "it as 'open a ticket for X'; that IS create_ticket, not a new type. Use 'assign_ticket' "
            "only when routing an existing ticket to a specific person. A request for access to a "
            "system or dataset (any tier) is also 'create_ticket': a Data Access Request titled "
            "'Data Access Request: <system>', naming the system, the dataset if given, the business "
            "reason, and the tier in target_tier (handbook section 8: access is granted only by the "
            "system's admins after an approved Data Access Request ticket exists; a chat approval "
            "alone is not enough). Use 'grant_access' only if the request is explicitly to grant "
            "access right now, bypassing that ticket. Otherwise -- specifically "
            "when the request itself is a security-sensitive or identity action policy is likely "
            "to forbid (e.g. disabling MFA, sharing credentials) -- name that action honestly "
            "(e.g. 'disable_mfa') rather than laundering it into 'create_ticket'; a separate "
            "policy layer decides whether it's allowed, not you."
        )
    )
    title: str
    description: str
    target_system: str | None = None
    system_as_written: str | None = Field(
        default=None,
        description=(
            "The words in the message that name or describe the target system, copied exactly as "
            "the requester wrote them (e.g. 'the Ledger billing dashboard'). Null if the message "
            "doesn't say which system -- a label you'd have to make up doesn't count."
        ),
    )
    target_tier: Literal["viewer", "operator", "admin"] | None = Field(
        default=None, description="For access requests: the access tier being requested."
    )
    assignee: str | None = None


class RequestState(TypedDict, total=False):
    run_id: str
    request_id: str
    requester_name: str
    requester_role: str
    requester_id: str | None  # verified Slack user id; None for seeded/web requests
    channel: str
    thread_ts: str
    message: str
    auto_approve: bool

    retrieved: list[dict]
    category: str
    original_category: str
    rationale: str
    confidence: float
    rationale_cites_unretrieved: list[int]  # sections the rationale cites that retrieval never surfaced

    answer: str
    cited_chunk_ids: list[str]
    grounded: bool
    grounding_reason: str

    proposed_action: dict
    permission_verdict: dict
    approved: bool
    approved_by: str
    approved_at: str
    approval: dict  # the approval record: who, when, decision, and the digest it's bound to
    ticket: dict
    action_summary: str

    handoff_reason: str
    final_outcome: str  # "answered" | "executed" | "escalated"
    final_response: str


# Calibrated on 77 distinct answers from the builder's eval runs (never the held-out set):
# answers overlapped their cited section 0.40-0.83, and the best-matching *other* section
# 0.00-0.36, so no single fixed floor separates them with room to spare. The test is relative
# instead: the cited section(s) must match the answer at least as well as any uncited section
# (within GROUNDING_TOLERANCE), plus a low absolute floor that still stops unrelated text.
GROUNDING_MIN_OVERLAP = 0.15
GROUNDING_TOLERANCE = 0.05


_SECTION_REF = re.compile(r"(?:\bsections?|\bsec\.?-?|§)\s*(\d{1,2}(?:\s*(?:/|,|and|&|or)\s*\d{1,2})*)", re.I)


def _section_numbers(text: str) -> set[int]:
    return {int(n) for m in _SECTION_REF.finditer(text or "") for n in re.findall(r"\d{1,2}", m.group(1))}


def _rationale_citations(rationale: str, retrieved: list[dict]) -> tuple[list[int], list[int]]:
    """Which handbook sections the classify rationale cites, and which of those the model
    never saw. A section counts as seen if it was retrieved or is cross-referenced inside
    a retrieved section (e.g. section 15 says "see Section 5"). Recorded, not routed on:
    the rationale is audit text, and the write itself is still gated and approved."""
    cited = _section_numbers(rationale)
    seen = {int(h["chunk_id"].split("-")[1]) for h in retrieved if h["chunk_id"].startswith("sec-")}
    for h in retrieved:
        seen |= _section_numbers(h["text"])
    return sorted(cited), sorted(cited - seen)


def _content_words(text: str) -> set[str]:
    return {w for w in _tokenize(text) if len(w) >= 3 and w not in _STOPWORDS}


def _grounding_ok(answer: str, cited_ids: list[str], retrieved: list[dict],
                  all_chunks: list | None = None) -> tuple[bool, str, float]:
    """A citation check with lexical tests -- not semantic entailment. The cited
    chunks must be among those retrieved; the answer's content words (stopwords
    removed) must overlap them at all; and no uncited handbook section may match the
    answer clearly better than what it cites. It catches unrelated answers and
    answers citing the wrong section; it can't catch an answer that reuses the
    right section's words to say something wrong."""
    retrieved_ids = {h["chunk_id"] for h in retrieved}
    if not cited_ids:
        return False, "No citations given for an answerable classification.", 0.0
    unknown = [c for c in cited_ids if c not in retrieved_ids]
    if unknown:
        return False, f"Cited chunk(s) {unknown} were not among the retrieved set.", 0.0
    words = _content_words(answer)
    if not words:
        return False, "The answer is empty.", 0.0

    def overlap_with(texts: list[str]) -> float:
        return len(words & _content_words(" ".join(texts))) / len(words)

    overlap = overlap_with([h["text"] for h in retrieved if h["chunk_id"] in cited_ids])
    if overlap < GROUNDING_MIN_OVERLAP:
        return False, f"Only {overlap:.0%} of the answer's content words appear in the cited section(s).", overlap
    sections = [(c.chunk_id, c.text) for c in (all_chunks or [])] or [(h["chunk_id"], h["text"]) for h in retrieved]
    rivals = [(overlap_with([t]), cid) for cid, t in sections if cid not in cited_ids and cid != "preamble"]
    if rivals:
        best, best_id = max(rivals)
        if best > overlap + GROUNDING_TOLERANCE:
            return False, (f"The answer matches {best_id} ({best:.0%}) better than the section(s) it cites "
                           f"({overlap:.0%})."), overlap
    return True, "ok", overlap


# Sampling can't be pinned on this model: the anthropic 1.x SDK has no
# `temperature` argument for claude-sonnet-5 at all, and thinking runs adaptive
# by default. Run-to-run variance is therefore measured (pass^k in
# evals/score.py) rather than suppressed, and recorded on every trace.
SAMPLING = {"model": config.MODEL_ID, "temperature": "not settable", "thinking": "adaptive (model default)"}


def action_digest(action: dict, run_id: str | None = None, request_id: str | None = None,
                  requester: str | None = None) -> str:
    """sha256 over the canonical JSON of the exact action arguments, bound to the
    run, the request and the requester. An approval carries this back: approve one
    thing for one person in one run, and only that thing can execute. It is not a
    secret -- who may approve is checked separately (approval_gate, server.py)."""
    payload = {"action": action, "run_id": run_id, "request_id": request_id, "requester": requester}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _bound_digest(state: dict, action: dict) -> str:
    return action_digest(action, state.get("run_id"), state.get("request_id"),
                         state.get("requester_id") or state.get("requester_name"))


def build_graph(bus: EventBus, index: HandbookIndex, board: BoardAdapter, checkpointer=None, llm=None):
    # `llm` is injectable so tests can force a model decision (e.g. propose a
    # forbidden action) and prove the code-level gate holds regardless.
    llm = llm or ChatAnthropic(model=config.MODEL_ID, max_tokens=1024, anthropic_api_key=config.ANTHROPIC_API_KEY)
    langfuse_handler = get_langfuse_handler()

    def _lf_config(state: RequestState, node: str) -> dict:
        cfg: dict = {
            "run_name": f"{node}:{state.get('request_id')}",
            "metadata": {
                "run_id": state.get("run_id"),
                "request_id": state.get("request_id"),
                "node": node,
                **{f"sampling_{k}": v for k, v in SAMPLING.items()},
            },
        }
        if langfuse_handler is not None:
            cfg["callbacks"] = [langfuse_handler]
        return cfg

    def _io_fields(sent, raw_message, schema) -> dict:
        """What the model was given and what it returned, for the bench's Model I/O view: the
        system block (full-handbook mode), the user prompt, the structured-output schema it was
        held to, and its raw reply. Masked by the bus like everything else it publishes."""
        msgs = sent if isinstance(sent, list) else [HumanMessage(content=sent)]
        system, messages = None, []
        for m in msgs:
            if m is None:
                continue
            content = m.content
            if isinstance(content, list):  # content blocks: keep the text, drop cache_control
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
            if isinstance(m, SystemMessage):
                system = content
            else:
                messages.append({"role": "user" if isinstance(m, HumanMessage) else m.type, "content": content})
        out = getattr(raw_message, "content", None)
        if isinstance(out, list):
            out = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in out)
        return {"system": system, "messages": messages, "output": out,
                "params": {"response_format": getattr(schema, "__name__", str(schema)),
                           "json_schema": schema.model_json_schema() if hasattr(schema, "model_json_schema") else None}}

    def _llm_call_event(node: str, raw_message, sent=None, schema=None) -> None:
        usage = getattr(raw_message, "usage_metadata", None) or {}
        details = usage.get("input_token_details") or {}
        in_tok = usage.get("input_tokens", 0)  # langchain's total: uncached + cache read + cache write
        cache_read = details.get("cache_read", 0) or 0
        # langchain-anthropic reports writes by TTL (ephemeral_5m/1h) and leaves "cache_creation" at 0.
        cache_write = (details.get("cache_creation", 0) or 0) + (details.get("ephemeral_5m_input_tokens", 0) or 0) \
            + (details.get("ephemeral_1h_input_tokens", 0) or 0)
        out_tok = usage.get("output_tokens", 0)
        cost = config.cost_for(config.MODEL_ID, in_tok - cache_read - cache_write, out_tok,
                               cache_read=cache_read, cache_write=cache_write)
        bus.publish(
            node=node,
            event_type="llm_call",
            model=config.MODEL_ID,
            input_tokens=in_tok,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_tokens=out_tok,
            cost_usd=round(cost, 6),
            sampling=SAMPLING,
            **(_io_fields(sent, raw_message, schema) if sent is not None else {}),
        )

    # RETRIEVAL_MODE=full: the whole handbook goes to classify and draft_answer as one
    # system block, marked for prompt caching, instead of the top-k retrieved sections.
    # It's the comparison a reviewer asks for at this size ("why retrieve from 15 sections?").
    full_handbook = getattr(index, "mode", None) == "full"
    handbook_system = SystemMessage(content=[{
        "type": "text",
        "text": "Northwire Technologies IT/Ops handbook. Each section is tagged with its chunk id.\n\n"
                + "\n\n".join(f"[{c.chunk_id}] {c.text}" for c in index.chunks),
        "cache_control": {"type": "ephemeral"},
    }]) if full_handbook else None

    def _handbook_context(state: RequestState) -> str:
        if full_handbook:
            return "(The full handbook is in the system prompt above, each section tagged with its chunk id.)"
        return "\n\n".join(f"[{h['chunk_id']}] {h['text']}" for h in state["retrieved"])

    def _ask(schema, prompt: str, state: RequestState, node: str, with_handbook: bool = False):
        """One structured model call. Returns the parsed object, or None if the
        output didn't parse -- every caller fails closed on None.

        method="json_schema" = the API's constrained structured outputs. With
        plain tool calling, draft_answer occasionally returned cited_chunk_ids
        as a string ("sec-9, sec-4") instead of a list and failed validation
        (3 of ~250 draft calls, found via the recorded parse errors)."""
        messages = [handbook_system, HumanMessage(content=prompt)] if (with_handbook and full_handbook) else prompt
        result = llm.with_structured_output(schema, include_raw=True, method="json_schema").invoke(
            messages, config=_lf_config(state, node)
        )
        raw = result["raw"]
        _llm_call_event(node, raw, sent=messages, schema=schema)
        if result["parsed"] is None:
            bus.publish(
                node=node,
                event_type="model_output_error",
                stop_reason=(getattr(raw, "response_metadata", None) or {}).get("stop_reason"),
                parsing_error=str(result.get("parsing_error"))[:500],
            )
        return result["parsed"]

    def ingest(state: RequestState) -> dict:
        bus.publish(node="ingest", event_type="node_enter", message=state["message"])
        return {}

    def retrieve(state: RequestState) -> dict:
        hits = index.retrieve(state["message"])
        bus.publish(node="retrieve", event_type="retrieval_hits", hits=hits)
        return {"retrieved": hits}

    def classify(state: RequestState) -> dict:
        context = _handbook_context(state)
        prompt = (
            "You are the triage step of an internal IT/Ops helpdesk agent for Northwire "
            "Technologies. A message arrived in #helpdesk-requests. Decide whether it is:\n"
            "- 'answerable': fully answerable from the handbook excerpts below, no system change needed. "
            "This includes questions *about* a change -- whether it needs approval, how it's done, who "
            "handles it -- and changes the handbook says the employee makes themselves: the requester "
            "asked, so the reply is the answer, not a ticket.\n"
            "- 'needs_write': the requester wants IT/Ops to make a system change (a ticket created/"
            "assigned, an access grant, or anything else that changes state) -- even if policy will "
            "ultimately forbid it. Name what they're actually asking for; do not pre-judge policy here.\n"
            "- 'escalate': you cannot determine the right answer from the handbook, the request is "
            "ambiguous or out of scope, or it involves a security/identity matter serious enough "
            "that a human should look at it regardless of what the handbook says. A request whose "
            "system, dataset or scope can't be identified from the message itself -- known only by who "
            "showed it, where it came up, or 'same access as someone else' -- is ambiguous: the handbook "
            "says not to guess, so a human asks which one.\n\n"
            "For 'needs_write', phrase the rationale as the decision itself (what the write is and "
            "why it needs a ticket/approval), not as a deferral -- the agent completes needs_write "
            "cases itself once approved; only 'escalate' hands off to a human.\n\n"
            f"Requester role: {state['requester_role']} (one of viewer/operator/admin)\n"
            f"Message: {state['message']}\n\n"
            f"Relevant handbook excerpts:\n{context}\n\n"
            "Give a rationale a human could audit this decision by, and a confidence 0-1."
        )
        parsed: ClassifyResult | None = _ask(ClassifyResult, prompt, state, "classify", with_handbook=True)
        if parsed is None:
            reason = "The triage model's output couldn't be read; routed to a human rather than guessed."
            bus.publish(node="classify", event_type="decision", category="escalate", original_category=None,
                        rationale=reason, confidence=0.0)
            return {"category": "escalate", "original_category": None, "rationale": reason,
                    "confidence": 0.0, "handoff_reason": reason}

        category = parsed.category
        handoff_reason = None
        if parsed.confidence < config.CONFIDENCE_THRESHOLD and category != "escalate":
            handoff_reason = (
                f"Low confidence ({parsed.confidence:.2f}) on '{parsed.category}'; "
                "routed to a human rather than guessed."
            )
            category = "escalate"

        cites, unseen = _rationale_citations(parsed.rationale, state["retrieved"])
        bus.publish(
            node="classify",
            event_type="decision",
            category=category,
            original_category=parsed.category,
            rationale=parsed.rationale,
            confidence=parsed.confidence,
            rationale_cites=cites,
            rationale_cites_unretrieved=unseen,
        )
        return {
            "category": category,
            "original_category": parsed.category,
            "rationale": parsed.rationale,
            "confidence": parsed.confidence,
            "handoff_reason": handoff_reason,
            "rationale_cites_unretrieved": unseen,
        }

    def draft_answer(state: RequestState) -> dict:
        context = _handbook_context(state)
        prompt = (
            "Answer this Northwire Technologies helpdesk request using ONLY the handbook "
            "excerpts below. Cite the chunk id(s) you relied on. If the excerpts don't actually "
            "support a confident answer, say so plainly rather than filling the gap.\n\n"
            f"Message: {state['message']}\n\nHandbook excerpts:\n{context}"
        )
        parsed: DraftAnswer | None = _ask(DraftAnswer, prompt, state, "draft_answer", with_handbook=True)
        if parsed is None:  # grounding_check then fails on the empty answer -> handoff
            return {"answer": "", "cited_chunk_ids": [],
                    "handoff_reason": "The drafted answer couldn't be read; routed to a human rather than sent."}
        return {"answer": parsed.answer, "cited_chunk_ids": parsed.cited_chunk_ids}

    def grounding_check(state: RequestState) -> dict:
        ok, reason, overlap = _grounding_ok(state["answer"], state.get("cited_chunk_ids", []), state["retrieved"],
                                            index.chunks)
        bus.publish(node="grounding_check", event_type="decision", grounded=ok, reason=reason,
                    overlap=round(overlap, 2), cited=state.get("cited_chunk_ids", []))
        return {"grounded": ok, "grounding_reason": reason}

    def propose_action(state: RequestState) -> dict:
        prompt = (
            "The requester wants a system change. Propose the concrete action, naming what they "
            "are actually asking for honestly (see the action_type field description) -- even if "
            "you suspect policy will forbid it. A separate policy layer decides that, not you.\n\n"
            f"Requester: {state['requester_name']} ({state['requester_role']})\n"
            f"Message: {state['message']}"
        )
        parsed: ProposedAction | None = _ask(ProposedAction, prompt, state, "propose_action")
        if parsed is None:  # permission_check fails closed on an unrecognized action type
            return {"proposed_action": {"action_type": "unparseable_model_output", "title": "", "description": ""}}
        return {"proposed_action": parsed.model_dump()}

    def permission_check(state: RequestState) -> dict:
        action = state["proposed_action"]
        verdict = permissions.permission_check(
            action["action_type"], state["requester_role"], target_tier=action.get("target_tier"),
            text=" ".join(str(action.get(k) or "") for k in ("title", "description", "target_system")),
        )
        if verdict["allowed"] and action.get("target_tier"):
            # An access request has to be for a system the requester actually named: the quote
            # must appear in their message, so a name the model made up can't reach the ticket.
            quote = (action.get("system_as_written") or "").strip()
            if not quote or quote.lower() not in state["message"].lower():
                verdict = {"allowed": False, "forbidden": False, "requires_approval": False, "unnamed_system": True,
                           "reason": ("The request doesn't say which system the access is for, so it's gone to "
                                      "a human to ask rather than guess (handbook section 11).")}
        bus.publish(node="permission_check", event_type="permission_verdict", action_type=action["action_type"], **verdict)
        out: dict = {"permission_verdict": verdict}
        if not verdict["allowed"]:
            out["handoff_reason"] = verdict["reason"]
        return out

    def approval_gate(state: RequestState) -> dict:
        verdict = state["permission_verdict"]
        action = state["proposed_action"]
        digest = _bound_digest(state, action)
        now = datetime.now()
        at = now.strftime("%-I:%M %p")

        def _record(decision: str, approver: dict, mode: str, presented: str | None, reason: str | None = None) -> dict:
            return {"decision": decision, "by": approver.get("name", "-"), "approver": approver,
                    "at": now.isoformat(timespec="seconds"), "mode": mode, "action_digest": digest,
                    "presented_digest": presented, "reason": reason, "action": action}

        if not verdict.get("requires_approval", True):  # a missing key must not skip the human
            none = {"name": "(no approval required)"}
            return {"approved": True, "approved_by": none["name"], "approved_at": at,
                    "approval": _record("not_required", none, "none", None)}
        if state.get("auto_approve"):
            auto = {"name": "auto (non-interactive run)", "role": "admin", "via": "auto"}
            bus.publish(node="approval_gate", event_type="approval_result", approved=True, mode="auto", action_digest=digest)
            return {"approved": True, "approved_by": auto["name"], "approved_at": at,
                    "approval": _record("approved", auto, "auto", digest)}

        # The server announces the pause (approval_requested) when it sees the interrupt;
        # publishing it here would fire again on resume, since the node re-runs from the top.
        decision = interrupt({"proposed_action": action, "permission_verdict": verdict, "action_digest": digest})
        decision = decision if isinstance(decision, dict) else {"approved": bool(decision)}
        approver = decision.get("approver") or {}
        approved = bool(decision.get("approved"))
        presented = decision.get("action_digest")
        reason = None
        if approved:
            if presented != digest:
                reason = ("The approval didn't match the exact action proposed for this request "
                          "(argument digest mismatch), so nothing was executed.")
            elif approver.get("role") not in config.APPROVER_ROLES:
                reason = "The person who approved this isn't an approver, so nothing was executed."
            elif approver.get("id") and approver.get("id") == state.get("requester_id"):
                reason = "Requesters can't approve their own requests, so nothing was executed."
        else:
            reason = f"{approver.get('name', 'The approver')} denied the proposed action, so nothing was executed."
        out: dict = {}
        if reason:
            approved = False
            out["handoff_reason"] = reason
        bus.publish(node="approval_gate", event_type="approval_result", approved=approved,
                    approved_by=approver.get("name"), approver_via=approver.get("via"), mode="human",
                    action_digest=digest, digest_match=presented == digest, reason=reason)
        return {**out, "approved": approved, "approved_by": approver.get("name", "unknown"), "approved_at": at,
                "approval": _record("approved" if approved else "denied", approver, "human", presented, reason)}

    def execute_action(state: RequestState) -> dict:
        action = state["proposed_action"]
        actor = state["requester_name"]
        approval = state.get("approval") or {}
        if approval.get("action_digest") != _bound_digest(state, action):
            # Belt and braces: execute only the exact arguments that were approved.
            reason = "The action changed after it was approved, so nothing was executed."
            bus.publish(node="execute_action", event_type="handoff", reason=reason)
            return {"final_outcome": "escalated",
                    "final_response": f"I've flagged this for a human on the IT/Ops team rather than acting on it myself — {reason}"}
        approval_note = (f"\n\nApproved by {state.get('approved_by', 'You')} at {state.get('approved_at', '')} "
                         f"(action sha256:{approval['action_digest'][:12]}).")
        description = action["description"] + approval_note
        approved = f"Approved by {state.get('approved_by', 'unknown')} at {state.get('approved_at', '')}."
        # One write per run, keyed by the run id: if the board drops mid-call and the call is
        # retried (or the resume is replayed), the ticket that already exists comes back.
        key = state["run_id"]
        if action["action_type"] == "assign_ticket" and action.get("assignee"):
            ticket = board.create_ticket(action["title"], description, created_by=actor, labels=["assigned"],
                                         assignee=action["assignee"], idempotency_key=key)
            summary = f"Opened {ticket['id']} and assigned it to {action['assignee']}: {ticket['title']}. {approved}"
        elif action.get("target_tier"):
            # grant_access never gets here (permission_check refuses it); access requests are tickets.
            ticket = board.create_ticket(action["title"], description, created_by=actor, labels=["data-access-request"],
                                         idempotency_key=key)
            summary = (f"Opened {ticket['id']}: {ticket['title']}. The system's data owner has to approve it "
                       f"before anyone grants access (handbook section 8). {approved}")
        else:
            ticket = board.create_ticket(action["title"], description, created_by=actor, labels=[], idempotency_key=key)
            summary = f"Opened {ticket['id']}: {ticket['title']}. IT/Ops will take it from here. {approved}"
        bus.publish(node="execute_action", event_type="tool_call", tool="board", transport=getattr(board, "transport", "local"), ticket=ticket)
        return {"ticket": ticket, "action_summary": summary}

    def handoff(state: RequestState) -> dict:
        reason = state.get("handoff_reason") or state.get("grounding_reason") or "Escalated for human review."
        bus.publish(node="handoff", event_type="handoff", reason=reason)
        return {
            "final_outcome": "escalated",
            "final_response": (
                "I've flagged this for a human on the IT/Ops team rather than acting on it myself "
                f"— {reason}"
            ),
        }

    def respond(state: RequestState) -> dict:
        if state.get("final_outcome"):
            return {}
        if state["category"] == "answerable" and state.get("grounded"):
            resp = state["answer"]
            outcome = "answered"
        elif state.get("ticket"):
            resp = state.get("action_summary") or f"Recorded {state['ticket']['id']}."
            outcome = "executed"  # the write happened on the board (see execute_action for what that means)
        else:
            resp = "I wasn't able to complete this."
            outcome = "escalated"
        bus.publish(node="respond", event_type="respond", outcome=outcome, response=resp)
        return {"final_outcome": outcome, "final_response": resp}

    graph = StateGraph(RequestState)
    for name, fn in [
        ("ingest", ingest),
        ("retrieve", retrieve),
        ("classify", classify),
        ("draft_answer", draft_answer),
        ("grounding_check", grounding_check),
        ("propose_action", propose_action),
        ("permission_check", permission_check),
        ("approval_gate", approval_gate),
        ("execute_action", execute_action),
        ("handoff", handoff),
        ("respond", respond),
    ]:
        graph.add_node(name, fn)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "retrieve")
    graph.add_edge("retrieve", "classify")

    graph.add_conditional_edges(
        "classify",
        lambda s: s["category"],
        {"answerable": "draft_answer", "needs_write": "propose_action", "escalate": "handoff"},
    )
    graph.add_edge("draft_answer", "grounding_check")
    graph.add_conditional_edges(
        "grounding_check", lambda s: "respond" if s["grounded"] else "handoff", {"respond": "respond", "handoff": "handoff"}
    )

    graph.add_edge("propose_action", "permission_check")
    graph.add_conditional_edges(
        "permission_check",
        lambda s: "approval_gate" if s["permission_verdict"]["allowed"] else "handoff",
        {"approval_gate": "approval_gate", "handoff": "handoff"},
    )
    graph.add_conditional_edges(
        "approval_gate",
        lambda s: "execute_action" if s["approved"] else "handoff",
        {"execute_action": "execute_action", "handoff": "handoff"},
    )
    graph.add_edge("execute_action", "respond")
    graph.add_edge("handoff", "respond")
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=checkpointer)
