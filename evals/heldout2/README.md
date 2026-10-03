# Held-out set #2 (written blind, after the fixes)

`requests.json`: 26 requests with expected category, action, board state and handbook section, plus the writer's reasoning for each.

## Why a second set

Held-out set #1 (`../heldout/`) exposed three agent failures (H1–H3). They were then fixed, and set #1 was re-run once. Its "after" number isn't a clean held-out number any more, because the fixes were aimed at its misses. This set was written after the fixes, by a session that knew nothing about them, so its score is the honest post-fix number. It is scored once and not tuned against.

## Who wrote it, and what it could see

A fresh Claude session (Opus), started 09-27 after the H1–H3 fixes were committed. It was given a copy of handbook version 1 (`data/handbook_v1.md`) in an otherwise empty folder, told to read nothing else, and given exactly the same brief as the writer of set #1 (the job paragraph is quoted in `../heldout/README.md`). It didn't see:
- the agent's code or prompts;
- the builder's 25 requests, held-out set #1, or the dev set;
- the weakness descriptions or the fixes.

Its tool calls were one read of the handbook, writing its file, and a few `python3 -c` checks that read only its own output. Like every subagent in the environment it was built in, it also received that environment's standing instructions, which say nothing about this project.

So it's independent of the builder and of the fixes, but still AI-written, not a person.

## Ambiguous cases

Three are marked `"ambiguous": true` and left out of the score:
- **hb-016:** the category is clear; the assignee vs `create_ticket` choice isn't.
- **hb-025:** an admin asks for a direct grant citing a manager's DM. Is that §11.5 escalate, or a §8 ticket for the colleague?
- **hb-026:** a travel notice. It could be an answer, or a ticket.

## Where the writer found handbook v1 unclear

Condensed from its notes. Five match what the set #1 writer found, independently:
- "credentials" (§2) vs routine unlocks (§7);
- verbal approval opening a ticket (§8) vs triggering escalation (§11.5);
- the clarifying question the agent can't ask (§11.3);
- "redirect" vs "must always go to a human" (§1, §11.4);
- no stated channel for travel notice (§9).

New ones:
- §11.5 cites "Sections 3, 8, and 9" as approval processes, but §9 has none.
- "Holiday on-call period" (§13) is never defined.
- One kind of request has three names: printer and hardware tickets are called "Facilities/Hardware" (§14) and "Hardware Request" (§6), while §2 sends physical issues to `#facilities`.
- Whether contractors need a separate VPN ticket: §4 vs §12.
- Whether an Operator can request access for a teammate (§3 only covers Viewers).
- Whether a stolen device also gets a replacement ticket after the security handoff (§6 vs §10/§11.1).

Handbook v2 (`data/handbook_v2.md`) already resolves the overlapping five. The new ones are listed in `data/handbook_changelog.md` as open.

## Running it

```bash
uv run evals/score.py --requests evals/heldout2/requests.json --k 3
```
