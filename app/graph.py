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
from typing import Callable, Literal, TypedDict

import agentlab as lab
from agentlab.langgraph import instrument
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from . import config, permissions
from .agent_lab import APP, STORY
from .board_adapter import CREATE_TICKET, BoardAdapter
from .events import EventBus
from .langfuse_sink import get_langfuse_handler
from .retrieval import CORPUS_ID, STOPWORDS as _STOPWORDS, HandbookIndex, _tokenize


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


def _in_words(chunk_ids: list[str]) -> str:
    """Cited chunk ids in plain words, for a check's detail: ': section 4 and section 9'."""
    names = [f"section {c[4:]}" if c.startswith("sec-") else "the opening note" if c == "preamble" else c
             for c in chunk_ids]
    if not names:
        return ""
    return ": " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


def _or(words: list[str]) -> str:
    """Roles in plain words: ['admin'] -> 'an admin'; ['admin', 'operator'] -> 'an admin or an operator'."""
    each = [("an " if w[:1] in "aeiou" else "a ") + w for w in words]
    return " or ".join(each)


def prompt_parts(sent) -> tuple[str | None, list[dict]]:
    """A prompt as plain parts: the system text (None without one) and the other messages as
    {role, content}. Content blocks keep their text and drop cache_control. The live view's Model
    I/O and the demo's replay model (app/demo/replay_model.py) both read prompts through this."""
    msgs = sent if isinstance(sent, list) else [HumanMessage(content=sent)]
    system, messages = None, []
    for m in msgs:
        if m is None:
            continue
        content = m.content
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
        if isinstance(m, SystemMessage):
            system = content
        else:
            messages.append({"role": "user" if isinstance(m, HumanMessage) else m.type, "content": content})
    return system, messages


def build_graph(bus: EventBus, index: HandbookIndex, board: BoardAdapter, checkpointer=None, llm=None,
                clock: Callable[[], datetime] = datetime.now):
    # `llm` is injectable so tests can force a model decision (e.g. propose a
    # forbidden action) and prove the code-level gate holds regardless. `clock` is the time the
    # approval is stamped with ("Approved by ... at 10:40 AM"); scripts/regen_demo.py passes the
    # recording's own clock, so the reply and the recorded approval tell the same time.
    llm = llm or ChatAnthropic(model=config.MODEL_ID, max_tokens=1024, anthropic_api_key=config.ANTHROPIC_API_KEY)
    langfuse_handler = get_langfuse_handler()

    def _lf_config(state: RequestState, node: str) -> dict:
        # No `callbacks` here: a callbacks list on an in-node call replaces the run's own, which would
        # hide this call from every handler on the graph (Agent Lab's included). Langfuse's handler
        # is on the graph itself (end of build_graph), so it sees this call through the run.
        return {
            "run_name": f"{node}:{state.get('request_id')}",
            "metadata": {
                "run_id": state.get("run_id"),
                "request_id": state.get("request_id"),
                "node": node,
                **{f"sampling_{k}": v for k, v in SAMPLING.items()},
            },
        }

    def _io_fields(sent, raw_message, schema) -> dict:
        """What the model was given and what it returned, for the live view's Model I/O: the
        system block (full-handbook mode), the user prompt, the structured-output schema it was
        held to, and its raw reply. Masked by the bus like everything else it publishes."""
        system, messages = prompt_parts(sent)
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
            stop_reason = (getattr(raw, "response_metadata", None) or {}).get("stop_reason")
            parsing_error = str(result.get("parsing_error"))[:500]
            bus.publish(node=node, event_type="model_output_error", stop_reason=stop_reason, parsing_error=parsing_error)
            lab.event("error", {"message": parsing_error, "type": "model_output_error", "stop_reason": stop_reason,
                                "retryable": True})
        return result["parsed"]

    k = config.RETRIEVAL_TOP_K
    search_words = {
        "hybrid": (f"A search, not the AI, picks the {k} parts of the handbook that best match the message, by "
                   f"matching both its words and its meaning. The AI only ever sees those {k}, never the whole handbook."),
        "bm25": (f"A keyword search, not the AI, picks the {k} parts of the handbook that best match the message's "
                 f"words. The AI only ever sees those {k}, never the whole handbook."),
        "full": "No search here: the AI is given the whole handbook with every request.",
    }
    mode = getattr(index, "mode", None) or "hybrid"
    what_it_reads = "the whole handbook" if full_handbook else f"the {k} handbook sections"

    @lab.step("Message arrives", actor="app",
              says="A request comes in from Slack, with who sent it and their access level (viewer, operator or admin).")
    def ingest(state: RequestState) -> dict:
        """Records the incoming message on the event bus. The request itself (who, their role, the
        message) is the run's input; nothing is changed here."""
        bus.publish(node="ingest", event_type="node_enter", message=state["message"])
        return {}

    @lab.step("Look up the handbook", actor="app", kind="retrieval", says=search_words.get(mode, search_words["hybrid"]))
    def retrieve(state: RequestState) -> dict:
        """HandbookIndex.retrieve over the message: BM25 fused with bge-small embeddings by reciprocal
        rank fusion (RETRIEVAL_MODE=hybrid), top RETRIEVAL_TOP_K sections, best first. Plain code."""
        hits = index.retrieve(state["message"])
        bus.publish(node="retrieve", event_type="retrieval_hits", hits=hits)
        lab.retrieved(CORPUS_ID, [{"id": h["chunk_id"], "title": h["section"], "score": h["score"], "text": h["text"],
                                   "bm25": h["bm25"]} for h in hits], query=state["message"])
        return {"retrieved": hits}

    @lab.step("Decide what kind of request", actor="ai", moment=True,
              says=(f"The AI reads the message, {what_it_reads} and the person's access level, and picks one of three "
                    "paths: answer it, open a ticket, or hand it to a person. It has to give a reason. As a backstop, if "
                    f"its own confidence score is below {config.CONFIDENCE_THRESHOLD}, the request goes to a person "
                    "whatever it picked."),
              paths={"answerable": lab.path("can answer it", says="It decided the handbook already answers this."),
                     "needs_write": lab.path("needs a change",
                                             says="It decided this needs something changed, which means a ticket."),
                     "escalate": lab.path("a person must decide",
                                          says="It decided this must go to a person. The AI won't act on it.")})
    def classify(state: RequestState) -> dict:
        """One structured call (ClassifyResult: category, rationale, confidence). A confidence under
        CONFIDENCE_THRESHOLD overrides any category but escalate to escalate; output that doesn't
        parse escalates too. Records which handbook sections the rationale cites and which of
        those retrieval never surfaced (audit only, not routed on)."""
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
            lab.decision(reason)
            lab.event("triage", {"picked": None, "routed": "escalate", "unseen": []})
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
        # The handbook's chunk ids (retrieval.py names sections sec-<n>), so the bench can match them to the corpus.
        cited_ids = [f"sec-{n}" for n in cites if n not in unseen]
        unseen_ids = [f"sec-{n}" for n in unseen]
        lab.decision(parsed.rationale, cited=cited_ids, confidence=parsed.confidence)
        lab.check("confidence_check", category == parsed.category, kind="confidence",
                  detail=("The AI was sure enough of its choice." if category == parsed.category
                          else "The AI wasn't confident enough in its choice, so it went to a person."),
                  words={"passed": "Passed", "failed": "Not sure enough: sent to a person"})
        lab.event("triage", {"picked": parsed.category, "routed": category, "unseen": unseen_ids})
        return {
            "category": category,
            "original_category": parsed.category,
            "rationale": parsed.rationale,
            "confidence": parsed.confidence,
            "handoff_reason": handoff_reason,
            "rationale_cites_unretrieved": unseen,
        }

    def route_after_classify(state: RequestState) -> Literal["answerable", "needs_write", "escalate"]:
        return state["category"]

    @lab.step("Write the answer", actor="ai",
              says="The AI writes a reply using only the handbook sections it was given, and names the ones it used.")
    def draft_answer(state: RequestState) -> dict:
        """One structured call (DraftAnswer: answer, cited_chunk_ids). Output that doesn't parse
        leaves an empty answer, which grounding_check then fails."""
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

    @lab.step("Check the answer", actor="rule", moment=True,
              says=("Before an answer is sent, code (not the AI) checks three things: that it cites only sections it "
                    "was actually given, that its wording comes from those sections, and that no other section fits "
                    "it better. This catches answers that wander off the handbook. It can't tell whether an answer "
                    "that uses the handbook's own words is still wrong."),
              not_needed="Not needed this time: the AI didn't write an answer.",
              paths={"grounded": lab.path("holds up", says="The answer held up against its sources, so it's sent."),
                     "not_grounded": lab.path("doesn't hold up", says=("The answer didn't hold up against its sources, "
                                                                       "so a person gets it instead."))})
    def grounding_check(state: RequestState) -> dict:
        """_grounding_ok: the cited chunks must have been retrieved, the answer's content words must
        overlap them by at least GROUNDING_MIN_OVERLAP, and no uncited section may match the answer
        better by more than GROUNDING_TOLERANCE. Lexical, not semantic entailment."""
        cited = state.get("cited_chunk_ids", [])
        ok, reason, overlap = _grounding_ok(state["answer"], cited, state["retrieved"], index.chunks)
        bus.publish(node="grounding_check", event_type="decision", grounded=ok, reason=reason,
                    overlap=round(overlap, 2), cited=cited)
        lab.check("grounded", ok, kind="grounding", evidence=cited if ok else (),
                  detail=(f"The answer's wording matches the handbook sections it cites{_in_words(cited)}." if ok else
                          f"Didn't pass: {reason} A person gets it instead of the answer being sent."))
        return {"grounded": ok, "grounding_reason": reason}

    def route_after_grounding(state: RequestState) -> Literal["grounded", "not_grounded"]:
        return "grounded" if state["grounded"] else "not_grounded"

    @lab.step("Fill in a ticket", actor="ai",
              says=("The AI drafts a ticket: what kind, which system, a title and a description. It sees the message "
                    "and who's asking, not the handbook. Nothing is created yet."))
    def propose_action(state: RequestState) -> dict:
        """One structured call (ProposedAction). It is told to name the action honestly even when
        policy may forbid it; output that doesn't parse becomes an unrecognized action type, which
        permission_check refuses."""
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

    @lab.step("Check the rules", actor="rule", moment=True,
              says=("Fixed rules, not the AI, decide whether this ticket may go ahead. What it can never do is never "
                    "allowed, whoever asks; the rules also screen the ticket's own wording for those, though a keyword "
                    "screen can be worded around. A request to grant access directly is refused (access only comes "
                    "through a ticket), and an access ticket must name a system the person actually wrote."),
              not_needed="Not needed this time: nothing was going to be changed.",
              paths={"allowed": lab.path("allowed", says="The rules allow it, and a person still has to approve."),
                     "forbidden": lab.path("refused", says="The rules refuse this, so a person gets it instead.")})
    def permission_check(state: RequestState) -> dict:
        """permissions.permission_check on the proposed action, the requester's role and the action's
        own wording (forbidden types and forbidden intents never pass). An allowed access ticket
        must also quote a system name that appears in the requester's message. No LLM."""
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
        # The plain words for what was asked for come from where the action is defined (the one
        # action the app can take, or the never list), so the story doesn't keep its own copy.
        asked_for = (CREATE_TICKET.title if action["action_type"] == CREATE_TICKET.id
                     else permissions.NEVER.words.get(action["action_type"]))
        lab.event("permission_verdict", {"action_type": action["action_type"], "asked_for": asked_for, **verdict})
        lab.check("permission", verdict["allowed"], kind="permission",
                  detail=(("Allowed. A person still has to approve it." if verdict.get("requires_approval", True)
                           else "Allowed.") if verdict["allowed"] else f"Not allowed: {verdict['reason']}"))
        out: dict = {"permission_verdict": verdict}
        if not verdict["allowed"]:
            out["handoff_reason"] = verdict["reason"]
        return out

    def route_after_permission(state: RequestState) -> Literal["allowed", "forbidden"]:
        return "allowed" if state["permission_verdict"]["allowed"] else "forbidden"

    @lab.step("A person approves", actor="person", moment=True,
              says=(f"A person sees the exact ticket and approves or denies it. Only {_or(sorted(config.APPROVER_ROLES))} "
                    "can approve, and never their own request. If anything in the ticket changed after they looked, "
                    "the approval doesn't count."),
              paths={"approved": lab.path("approved", says="A person approved this exact ticket."),
                     "denied": lab.path("said no", says="A person said no, so nothing was created.")})
    def approval_gate(state: RequestState) -> dict:
        """interrupt() with the proposed action, the verdict and the action's digest (bound to the
        run, request and requester). The resume must carry the same digest, from an approver role,
        and not from the requester; anything else counts as denied. auto_approve skips the pause
        for non-interactive runs."""
        verdict = state["permission_verdict"]
        action = state["proposed_action"]
        digest = _bound_digest(state, action)
        now = clock()
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
            lab.gate_resolved(True, by=auto)
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
        lab.gate_resolved(approved, by=approver or None, reason=reason)
        return {**out, "approved": approved, "approved_by": approver.get("name", "unknown"), "approved_at": at,
                "approval": _record("approved" if approved else "denied", approver, "human", presented, reason)}

    def route_after_approval(state: RequestState) -> Literal["approved", "denied"]:
        return "approved" if state["approved"] else "denied"

    @lab.step("Open the ticket", actor="app", kind="tool",
              says="The app opens the ticket exactly as it was approved. Opening a ticket is the only change it can make.")
    def execute_action(state: RequestState) -> dict:
        """Creates the ticket on the board with the exact approved arguments, keyed by the run id
        (a retried or replayed call returns the ticket that already exists). Refuses if the action's
        digest no longer matches the approval."""
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
        transport = getattr(board, "transport", "local")
        bus.publish(node="execute_action", event_type="tool_call", tool="board", transport=transport, ticket=ticket)
        lab.event("tool_call", {"tool": "board", "transport": transport, "ticket": ticket})
        return {"ticket": ticket, "action_summary": summary}

    @lab.step("Hand to a person", actor="app", moment=True,
              says=("The AI stops here and takes no action. It replies in the person's Slack thread itself: someone "
                    "on the IT/Ops team will take it from here."))
    def handoff(state: RequestState) -> dict:
        """Writes the hand-off reply from the first reason on record (the handoff reason, else the
        grounding reason) and marks the run escalated."""
        reason = state.get("handoff_reason") or state.get("grounding_reason") or "Escalated for human review."
        bus.publish(node="handoff", event_type="handoff", reason=reason)
        return {
            "final_outcome": "escalated",
            "final_response": (
                "I've flagged this for a human on the IT/Ops team rather than acting on it myself "
                f"— {reason}"
            ),
        }

    @lab.step("Reply in Slack", moment=True,  # its kind (terminal: every exit ends the run) is derived
              says=("The person who asked gets the reply in their Slack thread: the answer, or the ticket that was "
                    "opened. (After a hand-off, the hand-off step has already replied.)"))
    def respond(state: RequestState) -> dict:
        """Sets the run's outcome and reply: a grounded answer is 'answered', an opened ticket
        'executed', anything else 'escalated'. A run that already has an outcome (a hand-off)
        keeps it."""
        if state.get("final_outcome"):
            lab.outcome(state["final_outcome"])
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
        lab.outcome(outcome)
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
        "classify", route_after_classify,
        {"answerable": "draft_answer", "needs_write": "propose_action", "escalate": "handoff"},
    )
    graph.add_edge("draft_answer", "grounding_check")
    graph.add_conditional_edges("grounding_check", route_after_grounding, {"grounded": "respond", "not_grounded": "handoff"})

    graph.add_edge("propose_action", "permission_check")
    graph.add_conditional_edges("permission_check", route_after_permission, {"allowed": "approval_gate", "forbidden": "handoff"})
    graph.add_conditional_edges("approval_gate", route_after_approval, {"approved": "execute_action", "denied": "handoff"})
    graph.add_edge("execute_action", "respond")
    graph.add_edge("handoff", "respond")
    graph.add_edge("respond", END)

    compiled = graph.compile(checkpointer=checkpointer)
    if langfuse_handler is not None:  # on the graph, so it sees every call inside it (see _lf_config)
        compiled = compiled.with_config(callbacks=[langfuse_handler])
    # Agent Lab: the map (steps, branches, the words above) is read from this graph, and every run
    # carries it. `lab.verify(lab_graph())` in tests/test_agent_lab.py keeps the words honest.
    return instrument(compiled, app=APP, story=STORY, lock="agentlab.lock.json")


def lab_graph():
    """The graph with stand-in parts, for Agent Lab's tooling (`python -m agentlab verify|lock
    app.graph:lab_graph`) and tests: nothing in it is ever invoked."""
    from pathlib import Path

    from .demo.replay_model import ReplayModel

    return build_graph(EventBus(run_id="agent-lab"), HandbookIndex(mode="bm25"), BoardAdapter(Path(":memory:")),
                       llm=ReplayModel(cassette={"calls": []}))
