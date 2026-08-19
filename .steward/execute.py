#!/usr/bin/env python3
"""Steward GHA-mode EXECUTOR glue (vendored — see .steward/README.md).

For one signal: build a JOB, run the INVESTIGATE-first executor (native `claude`,
the real investigate/fix contracts) against the checked-out repo, and write the
result (diagnosis + route, and a PR for mechanical/verified fixes) to the
steward-state branch. diagnosis-only — the common case — writes the diagnosis
and opens NO PR; the morning surfaces it.

Env: GH_TOKEN, GITHUB_REPOSITORY, STEWARD_SIG/SIGNATURE/SAMPLE,
     STEWARD_TEST_CMD (default 'uv run pytest -q'),
     STEWARD_AUTO_FIX (1=fix routes open a PR; 0=diagnosis-only — docs/68),
     CLAUDE_CODE_OAUTH_TOKEN | ANTHROPIC_API_KEY (+ ANTHROPIC_BASE_URL for economy)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gha_executor as gx       # noqa: E402
import state_branch            # noqa: E402

CONTRACTS = os.path.join(HERE, "contracts")


def main() -> int:
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    sb = state_branch.StateBranch(".state", remote_url=url).ensure()

    # signal → JOB (the detect-job shape the investigate contract expects)
    job = {
        "sig": os.environ["STEWARD_SIG"],
        "title": os.environ.get("STEWARD_SIGNATURE", "") or "unclassified signal",
        "kind": "bug",
        "severity": "error",
        "evidence": {"shape": os.environ.get("STEWARD_SIGNATURE", ""),
                     "sample": os.environ.get("STEWARD_SAMPLE", "")},
        "why_it_matters": "a recurring error surfaced by the scheduled run",
        "suggested_action": "investigate",
    }

    # Suite command resolution, in trust order:
    #   1. explicit config (STEWARD_TEST_CMD set non-empty) — the user said so;
    #   2. steward_core.suite_cmd_for(detect_stack(...)) — the repo's own manifest decides, and it
    #      may honestly answer "" (java/ruby/ts have no discovery one-liner; verify already treats
    #      an empty suite as "skipped", which is the truthful verdict);
    #   3. the historical default, ONLY when the detector is absent (an older vendored core).
    # The old line hardcoded `uv run pytest -q` for every repo, so a Go or Node project got a
    # correct diagnosis + working fix reported back as "fix NOT verified" — the suite step failed
    # for a reason unrelated to the fix, deterministically, on every run.
    test_cmd = os.environ.get("STEWARD_TEST_CMD", "").strip()
    if not test_cmd:
        try:
            import steward_core as core
            # suite_cmd_for takes the WORKTREE and detects the stack itself; it may honestly
            # return "" (no discovery runner for this stack), which verify reports as skipped.
            test_cmd = core.suite_cmd_for(".")
        except (ImportError, AttributeError):
            test_cmd = "uv run pytest -q"   # pre-detector vendored core: keep the old behaviour
    # auto_fix gate (docs/68): the SAME config field as local — 0 → diagnosis-only (the default product;
    # investigate still runs, no PR is opened), 1 → fix routes open a PR. Maps onto run_investigation's
    # existing open_pr knob, so the executor itself is unchanged across placements.
    auto_fix = os.environ.get("STEWARD_AUTO_FIX", "1").strip() not in ("0", "false", "no", "")
    res = gx.run_investigation(
        ".", job, test_cmd=test_cmd,
        investigate_agent=gx.ClaudeAgent(os.path.join(CONTRACTS, "investigate.md")),
        fix_agent=gx.ClaudeAgent(os.path.join(CONTRACTS, "fix.md")),
        open_pr=auto_fix, dry_run=False,
    )

    sb.write_result(res.get("sig") or job["sig"], res)
    sb.commit_push(f"investigate: {job['sig']} -> route={res.get('route')} status={res.get('status')}")
    print(json.dumps(res, indent=2))
    return 0   # a result was recorded regardless of route/outcome


if __name__ == "__main__":
    raise SystemExit(main())
