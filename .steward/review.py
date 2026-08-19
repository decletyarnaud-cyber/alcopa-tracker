#!/usr/bin/env python3
"""Steward GHA-mode REVIEW glue (vendored — see .steward/README.md).

The GUI-DOWN half of the morning digest (Henry's intent): open the review locally in a terminal when
the user's machine GUI is up, and OTHERWISE drive it from GHA with remote-control. This is the
otherwise. When the user's laptop is off, the findings still live on the steward-state branch — this
job reads them, composes the review, and:

  1. ALWAYS delivers the review to the Actions run summary ($GITHUB_STEP_SUMMARY) — so the user sees
     their morning digest on their phone (GitHub mobile / web) even with no machine on and no bot set up.
  2. If a claude.ai credential is present (CLAUDE_CODE_OAUTH_TOKEN), ALSO opens a REMOTE-CONTROL claude
     session seeded with the review prompt and surfaces its claude.ai/code URL — the user drives the
     interactive review from their phone, exactly like the local Terminal.app session but hosted here.

Design choices that matter:
  - Self-contained: imports only `state_branch` (already vendored) — no config, no queue machinery.
    In CI there is no ~/.config and no local pipeline state; the findings ARE the state branch.
  - Fail-soft + bounded: a missing credential degrades to summary-only (never an error); the
    remote-control leg is time-boxed so the job can't hold a runner indefinitely.
  - On-demand by design: this job runs on a `workflow_dispatch` (the user taps "run" from the GitHub
    mobile app when they're ready), so no runner is held waiting on a schedule.

Env: GH_TOKEN, GITHUB_REPOSITORY, GITHUB_STEP_SUMMARY,
     CLAUDE_CODE_OAUTH_TOKEN (enables the remote-control leg; absent → summary only),
     STEWARD_RC_MODEL (default 'sonnet'), STEWARD_RC_TIMEOUT (seconds the session may run, default 3600).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# state_branch is co-located when VENDORED (.steward/); in the source tree it lives in tools/ (parent) —
# add both so the selftest runs from the repo and the glue runs from .steward without special PYTHONPATH.
_parent = os.path.dirname(HERE)
if _parent not in sys.path:
    sys.path.insert(0, _parent)
import state_branch  # noqa: E402 — vendored alongside


def _decided_sigs(sb) -> set:
    """Signatures already dispositioned by the human (merged/dismissed), read from the branch's
    handled.jsonl if present. Fail-soft: on any read error, decide nothing is decided (surface all)."""
    out: set = set()
    try:
        p = os.path.join(sb.dir, "handled.jsonl")
        if os.path.exists(p):
            for ln in open(p):
                ln = ln.strip()
                if not ln:
                    continue
                d = json.loads(ln)
                # handled.jsonl records observation, not human decision — keep surfacing; a future
                # decisions.jsonl (human dispositions) would be filtered here. Kept as the seam.
    except Exception:
        return set()
    return out


def _pending(results: dict) -> list[dict]:
    """The findings that still want a human: a diagnosis awaiting a call, or a verified fix awaiting
    merge/apply. Ordered newest-cursor-last so the summary reads chronologically."""
    out = []
    for sig, r in (results or {}).items():
        if not isinstance(r, dict):
            continue
        st = r.get("status")
        if st in ("diagnosis", "verified", "verify-in-prod"):
            out.append(r)
    return out


def render_summary(repo: str, pending: list[dict]) -> str:
    """The human-facing digest for the Actions run summary (Markdown). What the user reads on their
    phone: how many findings, and for each, the symptom + the call to make."""
    name = repo.split("/")[-1]
    if not pending:
        return (f"## 🛡 The Steward — morning review · `{name}`\n\n"
                f"Nothing pending — no findings await a decision. ✓\n")
    return _render_summary_body(name, pending)


def _render_summary_body(name: str, pending: list[dict]) -> str:
    diagnoses = [r for r in pending if r.get("status") == "diagnosis"]
    fixes = [r for r in pending if r.get("status") in ("verified", "verify-in-prod")]
    lines = [f"## 🛡 The Steward — morning review · `{name}`", "",
             f"**{len(pending)} finding(s) awaiting your decision** — "
             f"{len(diagnoses)} diagnosis, {len(fixes)} verified fix.", ""]
    for i, r in enumerate(pending, 1):
        title = (r.get("title") or r.get("signature") or "unclassified").strip()
        kind = "🔧 verified fix" if r.get("status") != "diagnosis" else "🔎 diagnosis"
        dg = r.get("diagnosis") or {}
        symptom = (dg.get("symptom") if isinstance(dg, dict) else None) or ""
        drift = (dg.get("what_drifted") if isinstance(dg, dict) else None) or ""
        need = r.get("decision_needed") or ("merge this verified fix?" if r.get("status") != "diagnosis"
                                            else "review this diagnosis and decide")
        lines.append(f"### {i}. {kind} — {title}")
        if symptom:
            lines.append(f"- **symptom:** {symptom}")
        if drift:
            lines.append(f"- **what drifted:** {drift}")
        if r.get("status") != "diagnosis":
            if r.get("diff_present") or r.get("diff"):
                lines.append(f"- a fix branch/diff exists on `steward-state` (apply it or enable "
                             f"'Allow Actions to create PRs')")
        opts = r.get("options") or []
        if isinstance(opts, list) and opts:
            lines.append("- **options:**")
            for o in opts[:4]:
                if isinstance(o, dict):
                    lbl = (o.get("option") or o.get("label") or "").strip()
                    mark = " ⭐ _(recommended)_" if o.get("recommended") else ""
                    lines.append(f"  - {lbl}{mark}")
                else:
                    lines.append(f"  - {o}")
        lines.append(f"- **decision:** {need}")
        lines.append("")
    return "\n".join(lines)


def render_prompt(repo: str, pending: list[dict]) -> str:
    """The prompt that seeds the REMOTE-CONTROL claude session. It does NOT pre-group or pre-decide —
    it hands claude the findings and the review contract; claude drives the user through each decision
    (one question at a time), exactly like the local morning session, and records decisions."""
    payload = json.dumps([
        {k: r.get(k) for k in ("sig", "title", "status", "route", "fix_class",
                               "decision_needed", "options", "diagnosis")}
        for r in pending
    ], indent=2)
    return (
        "You are the user's Steward morning-review session, driven remotely from their phone. Their "
        f"repository is {repo}. The findings below were produced autonomously by the Steward's GitHub "
        "Actions loop and are waiting for the user's decision.\n\n"
        "Walk the user through EACH finding, one at a time. For a diagnosis: explain the symptom and "
        "what drifted in plain language, present the options, and ask ONE clear question for their "
        "decision. For a verified fix: summarise the change and ask whether to apply/merge it. Group "
        "findings that share a root cause into a single decision. Do not summarise everything up front "
        "— take the decisions interactively. When a decision is made, state clearly what will happen "
        "next (open a PR, apply a diff, dismiss). Keep it tight and respect their time.\n\n"
        f"FINDINGS ({len(pending)}):\n{payload}\n"
    )


def _append_summary(text: str) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        try:
            with open(p, "a") as f:
                f.write(text + "\n")
        except Exception:
            pass
    # Always echo to the log too, so the review is visible even without the summary panel.
    print(text)


def _launch_remote_control(prompt: str) -> str | None:
    """Start a remote-control claude session seeded with the review prompt; return its claude.ai/code
    URL (or None). The session KEEPS RUNNING after this returns — the user drives it from claude.ai —
    up to STEWARD_RC_TIMEOUT. Fail-soft: any launch problem returns None and the caller degrades to
    summary-only. Requires CLAUDE_CODE_OAUTH_TOKEN (claude.ai auth) in the environment.

    NOTE: this leg runs an interactive claude session in a CI runner. It is guarded by a timeout so the
    job cannot hold a runner past the budget. It has NOT been validated in a live CI run yet — the local
    review path (Terminal.app) and the summary delivery above are the proven surfaces.
    """
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return None
    model = os.environ.get("STEWARD_RC_MODEL", "sonnet")
    try:
        timeout = max(60, int(os.environ.get("STEWARD_RC_TIMEOUT", "3600")))
    except ValueError:
        timeout = 3600
    log = os.path.join(HERE, "_rc_session.log")
    try:
        # --remote-control surfaces a claude.ai/code URL and relays the session to the user's phone.
        # -p seeds the prompt; the process stays alive while the user drives. Detach stdout to a log we
        # poll for the URL. IR_ALLOW_NESTED guards against a nested-CC false-exit in some environments.
        env = dict(os.environ, IR_ALLOW_NESTED="1")
        proc = subprocess.Popen(
            ["claude", "--remote-control", "--model", model, "-p", prompt],
            stdout=open(log, "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        return None
    # Poll the log for the session URL (up to ~90s to register), then let the session run.
    url = None
    for _ in range(30):
        time.sleep(3)
        try:
            m = re.search(r"https://claude\.ai/code/session_[A-Za-z0-9]+", open(log).read())
        except Exception:
            m = None
        if m:
            url = m.group(0)
            break
        if proc.poll() is not None:      # the CLI exited before surfacing a URL
            break
    if url is None:
        try:
            proc.terminate()
        except Exception:
            pass
        return None
    # Bound the session so the job can't hold the runner forever; the user drives it within this window.
    def _reap():
        deadline = time.time() + timeout
        while time.time() < deadline and proc.poll() is None:
            time.sleep(15)
        if proc.poll() is None:
            proc.terminate()
    import threading
    threading.Thread(target=_reap, daemon=True).start()
    return url


def main() -> int:
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("GITHUB_REPOSITORY not set", file=sys.stderr)
        return 2
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    try:
        sb = state_branch.StateBranch(".state", remote_url=url).ensure()
        sb.pull()
        results = sb.results()
    except Exception as e:
        _append_summary(f"## 🛡 The Steward — morning review\n\nCould not read the state branch "
                        f"({type(e).__name__}). Nothing to review.\n")
        return 0
    pending = _pending(results)
    _append_summary(render_summary(repo, pending))
    if not pending:
        return 0
    rc_url = _launch_remote_control(render_prompt(repo, pending))
    if rc_url:
        _append_summary(f"\n---\n### 📱 Drive this review from your phone\n\n"
                        f"An interactive Steward session is live — open it on claude.ai/code:\n\n"
                        f"**{rc_url}**\n\n_(the session stays open for your review window)_\n")
    else:
        _append_summary("\n---\n_To drive this review interactively from your phone, set the "
                        "`CLAUDE_CODE_OAUTH_TOKEN` secret (`claude setup-token`). Without it, the "
                        "digest above is delivered read-only._\n")
    return 0


def _selftest() -> int:
    f: list = []
    ck = lambda c, m: (None if c else f.append(m))
    diag = {"sig": "a1", "status": "diagnosis", "title": "405 on /recherche",
            "decision_needed": "remove fallback or POST?",
            "diagnosis": {"symptom": "405 Not Allowed", "what_drifted": "GET→POST"},
            "options": [{"option": "remove it", "recommended": True}, {"option": "POST it"}]}
    fix = {"sig": "b2", "status": "verified", "title": "stale salle URL", "diff_present": True,
           "diagnosis": {"symptom": "recurring 405"}}
    results = {"a1": diag, "b2": fix, "c3": {"status": "success"}}  # success is NOT pending

    pending = _pending(results)
    ck([r["sig"] for r in pending] == ["a1", "b2"], "pending = diagnosis + verified, excludes success")

    s = render_summary("owner/repo", pending)
    ck("2 finding(s)" in s and "1 diagnosis, 1 verified fix" in s, "summary counts diagnosis+fix")
    ck("405 on /recherche" in s and "remove it ⭐" in s, "summary renders title + recommended option")
    ck("`repo`" in s, "summary uses the repo NAME, not the owner/repo slug")
    ck("Nothing pending" in render_summary("o/r", []), "empty → 'Nothing pending' (no crash)")

    p = render_prompt("owner/repo", pending)
    ck("driven remotely from their phone" in p and "one at a time" in p.lower(), "prompt is the RC contract")
    ck('"sig": "a1"' in p and '"sig": "b2"' in p, "prompt carries the findings as JSON")
    ck("one question" in p.lower() or "ONE clear question" in p, "prompt asks one decision at a time")

    # remote-control leg is a NO-OP without the claude.ai token (degrades to summary-only, never errors)
    _tok = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    try:
        ck(_launch_remote_control("x") is None, "no OAuth token → remote-control leg is a fail-soft no-op")
    finally:
        if _tok is not None:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = _tok

    if f:
        print("review selftest FAILED:")
        for x in f:
            print("  -", x)
        return 1
    print("PASS review: pending-filter · phone summary (counts, recommended option, empty-safe) · "
          "remote-control prompt (RC contract + findings json) · RC leg fail-soft without a token")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    raise SystemExit(main())
