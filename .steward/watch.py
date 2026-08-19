#!/usr/bin/env python3
"""Steward GHA-mode WATCHER glue (vendored — see .steward/README.md).

Reads a run-log, computes novel error signatures (deduped against the
steward-state branch's handled-list), records them + advances the cursor on the
state branch, and emits a fan-out matrix for the execute job.

Usage: python .steward/watch.py <run-log-file>
Env:   GH_TOKEN (contents:write), GITHUB_REPOSITORY, GITHUB_OUTPUT, GITHUB_RUN_ID
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gha_signal          # noqa: E402
import gha_executor as gx   # noqa: E402
import state_branch        # noqa: E402


def _state() -> "state_branch.StateBranch":
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    return state_branch.StateBranch(".state", remote_url=url).ensure()


def main() -> int:
    log_path = sys.argv[1] if len(sys.argv) > 1 else ""
    # A watch with nothing to read must SAY so — a missing/unset signal source read as
    # empty produced a permanently-quiet watch that looked healthy (F5). ::warning:: puts
    # it on the run's annotations where a repo owner actually looks.
    if not log_path or not os.path.exists(log_path):
        print(f"::warning title=steward watch has no signal::signal source "
              f"{log_path!r} is unset or missing — the watch reads nothing and will stay "
              f"quiet forever. Set projects[<repo>].run_cmd (live capture), gha.signal "
              f"source 'shipped', or gha.signal.path (committed log), then re-vendor.")
    log = ""
    if log_path and os.path.exists(log_path):
        log = open(log_path, encoding="utf-8", errors="replace").read()

    sb = _state()
    # Suppress a signal only once it's SETTLED — a terminal result or a capped retry (C1) — EXCEPT a
    # MERGED fix that recurs in this run-log: that didn't hold and comes back as a regression (C9, the
    # 'did it hold' check on the shared resolution model). handled.jsonl is the attempt log.
    settled = gx.suppressed_sigs(sb.results(), sb.handled_counts(), sb.decisions())
    signals = gha_signal.novel(log, settled)

    # A capped signal with NO terminal result is a signal the executor failed on three times and
    # went quiet about — indistinguishable from success unless said out loud. Say it every run, as a
    # workflow annotation, so a broken credential/exec path cannot silently consume the repo's bugs.
    burned = gx.burned_out(sb.results(), sb.handled_counts())
    for sig in sorted(burned):
        print(f"::warning title=Steward signal burned out::{sig} hit the attempt cap with no result "
              f"— the executor is failing (credential? build?), not the repo being healthy")

    # single-writer: the watcher owns the attempt log + cursor
    for s in signals:
        sb.add_handled(s["sig"])   # record this dispatch attempt
    sb.set_cursor(os.environ.get("GITHUB_RUN_ID"))
    sb.commit_push(f"watch: {len(signals)} new signal(s)")

    matrix = {"include": [
        {"sig": s["sig"], "signature": s["signature"][:300], "sample": s["sample"][:300]}
        for s in signals
    ]}
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"has_signals={'true' if signals else 'false'}\n")
            f.write(f"matrix={json.dumps(matrix)}\n")
    print(f"[watch] {len(signals)} novel signal(s): {[s['sig'] for s in signals]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
