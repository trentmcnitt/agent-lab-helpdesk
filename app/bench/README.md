# The Slack Helpdesk Agent on Agent Lab

Agent Lab draws this app's flow, prompts, documents, checks and cost beside it, from OpenTelemetry its library (`agentlab`, a path dependency on a clone of [trentmcnitt/agent-lab](https://github.com/trentmcnitt/agent-lab) beside this repo, at `../agent-lab/sdk/python`) emits. Nothing it shows is typed twice, so nothing here can drift from the code:

| what Agent Lab shows | where it comes from |
|---|---|
| steps, branches, branch names | the compiled graph (`instrument(...)` at the end of `build_graph`, `app/graph.py`) |
| a step's plain name and description | `@lab.step(...)` on its node function; the function's docstring is its Engineering description |
| a branch's plain words | `paths={branch: lab.path(...)}` on the step it leaves, keyed by the real branch names |
| the handbook and its sections | `lab.corpus` in `HandbookIndex` (`app/retrieval.py`), from the index's own chunks |
| what the search returned | `lab.retrieved` in `retrieve` |
| the decision, the confidence backstop | `lab.decision` and `lab.check("confidence_check", ...)` in `classify` |
| the checks | `lab.check` in `grounding_check` and `permission_check` |
| the approval | derived from `interrupt()`; `lab.gate_resolved` after it returns |
| model calls, tokens, cost | LangChain's callbacks; cost from `app/agent_lab.py`'s `price` over `config.cost_for` |
| what it can never do | `permissions.NEVER` (`lab.never`), beside the rules, keyed by `config.FORBIDDEN_ACTION_TYPES` |
| the one action it can take | `board_adapter.CREATE_TICKET` |
| the request, who asked, the reply | `APP`'s `request="message"`, `requester=(...)`, `reply="final_response"` in `app/agent_lab.py`: the state's own field names, checked by `lab.verify` against the graph's input and output schema |
| name, description, privacy note, baseline, track record | `app/agent_lab.py` (`APP`); the baseline from `config.MANUAL_ESTIMATE`, the track record from the newest held-out eval in `evals/results/` |
| custom panels | `story.js` (this folder), its panels declared in `app/agent_lab.py` (`STORY`) |

The app's own facts the story draws (`triage` in `classify`, `permission_verdict` in `permission_check`, `tool_call` in `execute_action`) are `lab.event(...)` lines next to the code that produces them.

## Running it beside the app

Start the bench from its checkout (`uv run uvicorn bench.server:app --port 8790`). `server.py`, `demo_server.py` and `cli.py` call `app.agent_lab.init()` at startup, which sends to `http://127.0.0.1:8790` with no settings and is a silent no-op when nothing listens. Set `AGENT_LAB_URL` only when the bench runs on another machine (`off` disables it). For the story, start the bench with `AGENT_LAB_STORIES="slack-helpdesk=<this repo>/app/bench/story.js"`: the bench serves it only when its sha256 matches the map the run carried. While editing the story, add `AGENT_LAB_STORIES_DEV=1` so the bench serves the file as it is now.

The public demo opened in the bench's side-by-side shell files a visitor's live runs under the shell's session (`bench_session`). Its static export (`scripts/export_static.py`) plays each recording's own bench events beside the page, on the same clock.

## Keeping the words honest

`tests/test_agent_lab.py` runs `lab.verify(lab_graph(), strict=True)`: any word naming a step or branch the code no longer has fails, as does a worded step whose code changed since its words were last confirmed. The fingerprint covers each worded node function (its `@lab.step` included) and its router, not helpers they call: a change in `app/retrieval.py`, say, doesn't flag `retrieve`'s words. After a deliberate change, re-read that step's words, then:

```bash
uv run python -m agentlab lock app.graph:lab_graph      # rewrites app/agentlab.lock.json
```

## The demo's recordings

`demo/cassettes/*.json` are the only recorded model outputs (the only thing that cost money). Everything else is made from them by running the real graph with `app/demo/replay_model.py` answering the model calls:

```bash
uv run scripts/regen_demo.py            # demo/replays/*.json and demo/bench-recordings/*.recording.jsonl
uv run scripts/export_static.py --out dist/slack-helpdesk
```

The bench recordings are written by the bench's own normalizer (`bench.record`). A prompt change makes `regen_demo.py` stop with "stale: prompt for <node> changed"; re-record with `scripts/record_cassettes.py` (paid model calls), then regenerate. `tests/test_agent_lab.py` checks that the checked-in files are what the cassettes make today, that they validate against the bench's schemas, and that the story renders every recording in both modes (`tests/story_driver.js` for Presentation leaks, and the bench's own harness):

```bash
node ../agent-lab/tests/story_harness.js --story app/bench/story.js demo/bench-recordings/*.recording.jsonl
```
