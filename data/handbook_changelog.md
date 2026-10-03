# Handbook changelog

The handbook is a synthetic fixture: the company, its systems and its policies are invented for this demo. Version 1 was written by the same session that built the agent. Version 2 fixes the places where version 1 was unclear or contradicted itself, as found by the blind held-out writer (`evals/heldout/README.md`).

Both versions are kept. The 25 builder requests (`data/seed_requests.json`), the held-out set (`evals/heldout/`) and the dev set (`evals/dev/`) are labeled against **version 1**, and the eval runs on version 1 unless `HANDBOOK_VERSION=2` is set. Relabeling against version 2 is a separate step, with its own record.

## Version 2 (September 2026)

Each change answers one of the blind writer's notes (numbered as in `evals/heldout/README.md`).

1. **Elevated access vs escalation** (§2). v1 said anything touching "security, credentials, or elevated access" follows the Escalation Policy, while §8 gave elevated access a normal ticket path. v2: security incidents, credentials/MFA, and access requests that come with pressure to skip the logged approval are escalations; a plain request for more access, at any tier, is a §8 ticket; an account unlock is a routine ticket.
2. **Verbal approval vs pressure** (§11, now item 4). v2 adds that pressure decides it: mentioning a manager's verbal OK while asking for the ticket to be opened follows §8; asking for the change itself to happen now on the strength of it is escalated.
3. **Clarifying questions** (§11, item 3). v1 said "ask a clarifying question or escalate", which the agent can't do. v2: the agent hands these to a human, who asks.
4. **Out-of-scope requests** (§1, §11). v1 listed them under "must always go to a human" and also said to "redirect" them. v2: the helpdesk replies pointing to the right team, and escalates only when it isn't clear which team. The item is moved out of the §11 list, so **§11's items 5 and 6 are renumbered 4 and 5**.
5. **Assignees** (§2). v1 never said when a ticket carries an assignee. v2: tickets are opened unassigned and IT Operations assigns them; a ticket is opened already assigned only when the requester asks for work to go to a named member of IT Operations.
6. **A Viewer asking for someone else's access** (§3). v2: the helpdesk explains that the person or their manager must request it; nothing is filed.
7. **Travel notice** (§9). v2: notify by opening a Waypoint "Travel Notice" ticket.
8. **A legitimate request mixed with an injected instruction** (§11, now item 5). v2: the whole message is escalated; no part is filed automatically.

### Still open in version 2

Found by the blind re-check (`evals/heldout/recheck_v2.json`). No held-out label depends on 3 or 4.

1. **General "when in doubt, escalate" vs the out-of-scope redirect.** The preamble and §1 still say anything not covered goes to a human, while §1's HR/Facilities/Legal rule now says reply and redirect. The re-checker let the specific rule win.
2. **"Anything touching credentials" (§2) vs self-service password resets (§7).** Only account unlocks are carved out. Read literally, the §2 wording could also catch VPN certificate renewals.
3. **§11 item 4 cites "the logged approval process in Sections 3, 8, and 9",** but §9 has no approval gate.
4. **§5 excludes software reimbursement "except as described in Section 15",** but §15 describes no software exception.

Found by the second blind writer (`evals/heldout2/README.md`), against version 1 and still present in version 2. Item 3 above was also found independently by this writer.

5. **"Holiday on-call period"** (§13) is never defined.
6. **One kind of request has three names:** "Facilities/Hardware" ticket (§14), "Hardware Request" (§6), and `#facilities` for physical issues (§2).
7. **Contractor VPN:** a separate ticket with a sponsoring manager (§4), or automatic with onboarding (§12)?
8. **An Operator requesting access for a teammate:** §3 only says Viewers can't.
9. **A stolen device:** does a replacement ticket follow the security handoff (§6 vs §10, §11)?
