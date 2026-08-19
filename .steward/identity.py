#!/usr/bin/env python3
"""The Steward's canonical git identity — single source of truth for commit attribution.

Every commit the AUTONOMOUS Steward makes (fix PRs, bridge PRs, goal-loops, the state branch) carries a
`Co-Authored-By: The Steward <email>` trailer, so the Steward is RECOGNIZED as a contributor on GitHub the
same way Claude is — by default, unless a human overrides the commit. Author stays the git user / triggerer;
the Steward is the CO-AUTHOR (the Claude model — least disruptive to `git blame`).

For the contributor GRAPH (avatar + count), the trailer email must map to a real GitHub account — a
dedicated Steward BOT account. Set it ONCE here, or via ~/.config/inferroute/the-steward.json `identity`:
    "identity": { "name": "The Steward", "email": "<bot-account-email>" }
The default below is a PLACEHOLDER — replace `email` with the bot account's address (typically the GitHub
noreply form `<id>+the-steward[bot]@users.noreply.github.com`) once the account exists. The config override
means the email can change without a code edit; CI (no config) falls back to this default, so the default
SHOULD be the real bot email once known.
"""
import json, os, sys
from pathlib import Path

_CONFIG = Path(os.path.expanduser("~/.config/inferroute/the-steward.json"))
# The Steward's branded identity. For contributor-GRAPH credit (avatar + count), this email must be a
# VERIFIED email on the Steward bot GitHub account; otherwise it shows as co-author TEXT on commits
# (still visible + greppable). Override per-machine via the-steward.json `identity.email`.
_DEFAULT = {"name": "The Steward", "email": "the-steward@inferroute.ai"}


def steward_identity() -> dict:
    """The {name, email} for the Steward's commits. The config `identity` block overrides the default, so
    the bot-account email is settable without a code change; CI (no config) uses the default."""
    try:
        idn = json.loads(_CONFIG.read_text()).get("identity") or {}
    except Exception:
        idn = {}
    return {"name": idn.get("name") or _DEFAULT["name"], "email": idn.get("email") or _DEFAULT["email"]}


def coauthor_trailer() -> str:
    """The `Co-Authored-By: The Steward <email>` trailer appended to every autonomous Steward commit."""
    i = steward_identity()
    return f"Co-Authored-By: {i['name']} <{i['email']}>"


def with_trailer(message: str) -> str:
    """`message` with the Steward co-author trailer appended. Idempotent (won't double-add) — safe to wrap
    a message that may already carry it. Blank-line separated so git parses it as a trailer."""
    t = coauthor_trailer()
    msg = message or ""
    if t in msg:
        return msg
    return msg.rstrip() + "\n\n" + t


def _selftest():
    i = steward_identity()
    assert i["name"] and "@" in i["email"], i
    t = coauthor_trailer()
    assert t.startswith("Co-Authored-By: ") and i["email"] in t and i["name"] in t, t
    m = with_trailer("steward: fix the 429 race")
    assert "fix the 429 race" in m and m.rstrip().endswith(t), m
    assert with_trailer(m) == m, "idempotent — never double-adds the trailer"
    print("PASS identity: steward_identity (config-overridable, CI-default) · coauthor_trailer · with_trailer idempotent")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest(); sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "coauthor":
        print(coauthor_trailer()); sys.exit(0)
    print("usage: identity.py coauthor | --selftest", file=sys.stderr); sys.exit(2)
