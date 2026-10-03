# Deploying the public demo

The public demo is `demo_server.py`, not `server.py`. It serves the same UI with no Slack and no operator endpoints. Every visitor gets their own sandbox (board, thread, event stream), keyed by a cookie. Nothing here has been deployed; this is the plan and the exact steps.

## Two modes

- **Replay (the default, and the one for a case-study page).** Recorded runs from `demo/replays/` play back through the same UI, the peek view included, with **no model calls and no API key**. The approval card still pauses the run: approving with the right digest opens the recorded ticket on the visitor's board, and anything else plays the denied ending. Memory peaks around 120 MB.
- **Live.** The real agent, on the visitor's request, behind four guards:
  - **Per-IP rate limits:** `DEMO_RATE_PER_MINUTE` and `DEMO_RATE_PER_DAY`.
  - **A cap on concurrent runs** (`DEMO_MAX_CONCURRENT`).
  - **A hard daily spend cap** (`DEMO_DAILY_USD`). Each run's recorded model cost is added to a ledger on disk, so it survives restarts. Once today's spend reaches the cap, the demo switches to replays until midnight UTC. Runs already in flight can overshoot the cap by at most `DEMO_MAX_CONCURRENT` × one run's cost (about $0.03).
  - **Scenario buttons only,** unless `DEMO_FREE_TEXT=1`, which allows 500-character requests, still rate-limited and capped.

  When a guard refuses, the visitor gets the recorded run and the page says why. Live mode loads the embedding model, so memory peaks around 660 MB and it needs a 1 GB instance.

All settings are env vars; `.env.demo.example` lists them with comments. Replays are re-recorded with `uv run scripts/record_replays.py` (about $0.10 for all eight).

## What was verified locally, and what wasn't

- Verified:
  - `tests/test_demo_limits.py` and `tests/test_demo_server.py`: the rate limits, the spend cap tripping and persisting, the fallback to replay, visitor isolation, another session being unable to approve your run, replay digests, and no free text or `/docs` by default.
  - The UI in a browser in replay mode: scenario, peek view, approval, ticket, and the "recorded run cost" label.
  - The app running from exactly the files the Dockerfile copies.
- **Not verified: `docker build`.** The local Docker VM couldn't reach Docker Hub tonight (a timeout fetching the auth token, while the Mac itself reached it fine). Run `docker compose up --build` once before relying on the image. Also untested: compose's `read_only: true` with the real image. If startup fails on a write (for example, fastembed touching its model cache), remove that line or add a tmpfs for the path it names.

## Options

Prices below were checked on 09-27-26. Fly's come from its pricing docs, which show no date; Render's free-tier behavior comes from its docs; **Render's paid price comes from third-party pages only**.

| | Fly.io | Render |
|---|---|---|
| Replay mode | shared-cpu-1x 256 MB ≈ $1.94/mo if always on. Stopped machines aren't billed for CPU/RAM, only rootfs (about $0.15/GB-month). With auto-stop, the bill is pennies. | Free tier: spins down after 15 min idle and takes about a minute to come back, a bad first impression from a portfolio link. Free instances can't have a disk, so the spend ledger would reset (fine in replay-only mode). |
| Live mode | 1 GB ≈ $7.78/mo always on, far less with auto-stop; volume $0.15/GB-month | Starter is $7/mo with 512 MB (third-party figure), which is **too small for live mode**; the next tier up is needed |
| Isolation | Separate VM, separate account | Separate |
| Custom subdomain + TLS | `fly certs add` + a CNAME | custom domain + a CNAME |

Model spend is separate from hosting and bounded by `DEMO_DAILY_USD`. At the default $1/day, the worst case is about $30/month, reached only if someone uses up the cap every day. Replay mode spends nothing.

## Recommendation

**Fly.io, in replay mode first,** on its own subdomain, linked from the case-study page. Why:
- Replay costs nothing to run, can't be abused into model spend, and shows the full trace.
- Auto-stop makes hosting nearly free.
- It keeps a public, internet-facing process off your own machines and away from your credentials. That isolation is the reason to pay a few dollars rather than self-host for free. The container runs as non-root with a read-only filesystem, but a separate host is still the cleaner line.

Turn on live mode later, deliberately, with:
- a 1 GB machine;
- an Anthropic key used only for this demo, ideally in its own workspace with a low spend limit if your account supports one;
- `DEMO_DAILY_USD` at $1.

## Exact steps (Fly.io)

All of these are yours to run; none has been run. Commands run from the repo root (or from a clone of the public snapshot).

1. **Check the image builds:** `docker compose up --build`. Open http://localhost:8080, click a scenario, open the peek view, and approve the JetBrains request.
2. **Install and log in:** `brew install flyctl`, then `fly auth login` (opens a browser).
3. **Create the app without deploying:** `fly launch --no-deploy --copy-config --name <app-name>`. Keep the region or pick one near your visitors. If it asks about a database, say no.
4. **Create the ledger volume:** `fly volumes create demo_data --size 1 --region <same region>`.
5. **Deploy:** `fly deploy`. Then open `https://<app-name>.fly.dev`, run a scenario, and check `/healthz`.
6. **Add the subdomain** (e.g. `demo.example.com`): `fly certs add demo.example.com`, add the CNAME it prints at your DNS provider, and wait for `fly certs show demo.example.com` to report the certificate issued.
7. **Link it** from your portfolio page.

**Later, to go live:**
1. `fly scale memory 1024`.
2. `fly secrets set ANTHROPIC_API_KEY=<demo-only key>`.
3. `fly secrets set DEMO_MODE=live` (or edit `[env]` in `fly.toml` and redeploy).
4. Watch spend in the Anthropic console for the first few days.

To go back: `fly secrets unset DEMO_MODE` (the `fly.toml` default is `replay`).
