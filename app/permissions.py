"""'The model proposes, the policy decides' (Decawork). Every function here
is deterministic code -- never an LLM call -- and fails closed on anything
it doesn't recognize rather than guessing."""
from __future__ import annotations

import re

from . import config

_MFA = r"(?:mfa|2fa|two[- ]?factor|multi[- ]?factor|authenticator)"
_WEAKEN = r"(?:disabl|turn(?:ing|ed)?\s+off|remov|bypass|skip|paus|reset|suspend|deactivat|exempt)"
_SECRET = r"(?:password|passcode|credential|api[- ]?key|token|secret)"
# Forbidden intents, matched against what the action actually says it does -- so a
# forbidden request relabeled as an innocuous action type is still refused. A
# keyword screen can be paraphrased around; it backs up the classifier and the
# human approver rather than replacing them. False positives fail safe (to a human).
FORBIDDEN_INTENTS = [
    (re.compile(rf"\b{_WEAKEN}\w*\b[^.\n]{{0,40}}\b{_MFA}\b|\b{_MFA}\b[^.\n]{{0,40}}\b{_WEAKEN}", re.I), "disable_mfa"),
    (re.compile(rf"\b(?:share|send|give|provide|tell|disclose)\w*\b[^.\n]{{0,40}}\b{_SECRET}s?\b", re.I), "share_credentials"),
]


def forbidden_intent(text: str) -> str | None:
    for pattern, name in FORBIDDEN_INTENTS:
        if pattern.search(text or ""):
            return name
    return None


def permission_check(action_type: str, requester_role: str, target_tier: str | None = None, text: str = "") -> dict:
    """Returns {allowed, forbidden, requires_approval, reason}.

    Deliberately does NOT hard-block a viewer from requesting access to another
    system -- that's a routine request (e.g. "give me read access to the
    Compass dashboard"), and the agent's part is opening the Data Access Request
    ticket, never the grant itself (handbook section 8). What matters is whether
    the action is structurally forbidden (disable_mfa etc., always), a direct
    grant (refused), or a ticket for admin-tier access, which is allowed to the
    approval gate but flagged for closer human scrutiny there -- a hardcoded
    role check can't tell a legitimate urgent admin grant from a
    social-engineering pressure play ("no time to explain, my manager already
    said it's fine"); that judgment belongs to classify's rationale and the
    human at the approval gate, not to this function.
    """
    if requester_role not in config.ROLES:
        return {
            "allowed": False,
            "forbidden": False,
            "requires_approval": False,
            "reason": f"Unrecognized requester role '{requester_role}'; routed to a human.",
        }

    disguised = forbidden_intent(text) if action_type not in config.FORBIDDEN_ACTION_TYPES else None
    if disguised:
        return {
            "allowed": False,
            "forbidden": True,
            "requires_approval": False,
            "reason": (
                f"This action's own description reads as '{disguised}', which is never permitted via chat "
                f"request, whatever the action is labeled ('{action_type}'). Routed to the security team."
            ),
        }

    if action_type in config.FORBIDDEN_ACTION_TYPES:
        return {
            "allowed": False,
            "forbidden": True,
            "requires_approval": False,
            "reason": (
                f"'{action_type}' is never permitted via chat request, regardless of role. "
                "Requires in-person or verified-video identity confirmation with the security "
                "team (handbook: Account Recovery & Multi-Factor Authentication Policy)."
            ),
        }

    if action_type in ("create_ticket", "assign_ticket"):
        reason = "Benign write; still requires human sign-off before executing."
        if target_tier == "admin":
            reason = "Ticket for admin-tier access; allowed to reach the approval gate, flagged for closer scrutiny there."
        return {
            "allowed": True,
            "forbidden": False,
            "requires_approval": True,
            "reason": reason,
        }

    if action_type == "grant_access":
        # Handbook section 8: access is granted only by the system's admins, after an approved
        # Data Access Request ticket exists; a chat or Slack approval alone isn't sufficient.
        # So a direct grant from a chat request goes to a human; the agent's route is the ticket.
        return {
            "allowed": False,
            "forbidden": False,
            "requires_approval": False,
            "reason": (
                "A direct access grant from a chat request isn't allowed: access is granted by the "
                "system's admins after an approved Data Access Request ticket exists (handbook section 8)."
            ),
        }

    return {
        "allowed": False,
        "forbidden": False,
        "requires_approval": False,
        "reason": f"Unrecognized action type '{action_type}'; routed to a human rather than guessed.",
    }
