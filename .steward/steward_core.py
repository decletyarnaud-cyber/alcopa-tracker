#!/usr/bin/env python3
"""Placement-neutral Steward policy — ONE source of truth shared by both placements (the local
bash pipeline and the gha Python executor), so honesty-critical rules can't silently drift.

Scope is DELIBERATELY narrow — only policy that is genuinely identical across placements:
  - the route / fix_class taxonomy + route derivation   (pipeline.sh ↔ gha_executor),
  - the no-silencing honesty scan                        (verify.sh   ↔ gha_executor),
  - the resolution model                                 (ledger.py deploy-anchored ↔ gha merge-anchored).

NOT here — legitimately placement-specific, unifying them would be over-engineering:
  - storage backend (local SQLite ledger vs gha git-branch JSONL),
  - signal source + masking (journald/get-logs vs run-log adapter),
  - inference transport (ir vs native claude), worktree-vs-CI-checkout.
See docs/62 §Unification for the share / guard / leave decisions.
"""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from pathlib import Path

# --- taxonomy: the route the investigate agent commits to · the defect's nature -------------
ROUTES = ("diagnosis-only", "worktree-reproduced-test", "verify-in-prod-patch")
FIX_CLASSES = ("mechanical", "config", "design-decision", "external", "capacity")


def derive_route(route: str | None = None, fix_class: str | None = None,
                 reproducibility: str | None = None) -> str:
    """The investigate agent commits to `route`; derive it from fix_class/reproducibility for results
    predating the field. `diagnosis-only` is the safe default. (Mirrors orchestrator/pipeline.sh:218–225;
    the drift-guard selftest below pins the same vectors.)"""
    r = (route or "").strip().lower()
    if r in ROUTES:
        return r
    fc = (fix_class or "mechanical").strip().lower()
    rk = (reproducibility or "worktree-reproduced").strip().lower()
    if fc != "mechanical":
        return "diagnosis-only"
    if rk == "worktree-blocked":
        return "verify-in-prod-patch"
    return "worktree-reproduced-test"


# --- the no-silencing honesty scan (verify.sh:silencing_scan ↔ gha verify) -------------------
# A fix that swallows the symptom (bare except / except: pass / return None) is what this catches.
# THE single definition — orchestrator/verify.sh and tools/gha_executor.py both call this.
_SILENCE_RE = re.compile(r"except\s*:|except\s+\w+\s*:\s*$|except[^\n]*:\s*\n\s*pass|return None\b")


def silencing_scan(diff_text: str):
    """(clean: bool, fired: list[str]) over the ADDED (+) lines of a unified diff."""
    added = "\n".join(l[1:] for l in (diff_text or "").splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    fired = sorted(set(_SILENCE_RE.findall(added)))
    return (not fired), fired


# --- the resolution model (ledger.py deploy-anchored ↔ gha merge-anchored) -------------------
# A fix is not "held" on a clock; it settles against a real DEPLOY/MERGE event and whether the
# symptom recurs after it. LOCAL anchors on a recorded deploy + the live symptom; GHA anchors on the
# PR merge landing in master + the signal disappearing from the next run. SAME policy, different anchor.
# This is the canonical spec; the FROZEN local ledger.py implements it (guarded below, not refactored),
# and the gha "did it hold" follow-up is built ON this rather than as a parallel model.
RESOLUTION_STATES = ("held", "awaiting-deploy", "persisting", "settling")


def resolution_state(*, recurred: bool, deployed: bool,
                     recurred_after_deploy: bool, within_settling: bool) -> str:
    """held | awaiting-deploy | persisting | settling. (Mirrors ledger.py cmd_verify / doc 60.)
      - not recurred                         → held (resolved).
      - recurred but NOT yet deployed/live   → awaiting-deploy (the fix isn't live; don't blame it).
      - recurred AFTER the deploy/merge      → persisting (the fix didn't hold → re-investigate).
      - recurred within the settling grace   → settling (deploy unknown; give reality time)."""
    if not recurred:
        return "held"
    if not deployed:
        return "awaiting-deploy"
    if recurred_after_deploy:
        return "persisting"
    return "settling" if within_settling else "persisting"


# --- fix-acceptance honesty rails (gha_executor verify ↔ verify.sh) --------------------------
# A fix is HONEST only if it actually changes the diagnosed source and doesn't game the test surface.
# Found by the in-session dogfood loop (docs/62) and validated on the farm's real-agent ground truth (a
# test-tamper gamed fix slipped the LOCAL verify.sh — F3). These live HERE so the gha executor AND verify.sh
# share ONE definition — the unification.
_COMMENT = re.compile(r"^\s*($|#|//|/\*|\*/?|--|<!--)")


def substantive_diff(diff: str) -> bool:
    """True iff the added lines include >=1 NON-blank, NON-comment line — a real change, not a no-op patch
    (empty / whitespace / comment-only). A 'fix' that changed nothing real isn't a fix."""
    for ln in (diff or "").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            body = ln[1:]
            if body.strip() and not _COMMENT.match(body):
                return True
    return False


def root_cause_files(inv: dict) -> set:
    """Files the diagnosis names as the fix target (the SUT a real fix must change)."""
    out: set = set()

    def grab(v):
        if isinstance(v, str):
            out.add(v.split(":")[0].lstrip("./"))
        elif isinstance(v, dict):
            for k in ("file", "path"):
                if isinstance(v.get(k), str):
                    out.add(v[k].split(":")[0].lstrip("./"))
        elif isinstance(v, list):
            for x in v:
                grab(x)

    grab((inv or {}).get("root_cause"))
    d = (inv or {}).get("diagnosis")
    if isinstance(d, dict):
        grab(d.get("root_cause"))
    return out


def changed_files(repo_dir: str) -> set:
    """Repo-relative paths the working tree changed vs HEAD (modified + new), via git status."""
    r = subprocess.run(["git", "-C", repo_dir, "status", "--porcelain"], capture_output=True, text=True)
    return {ln[3:].strip() for ln in r.stdout.splitlines() if ln[3:].strip()}


def fix_touched_root_cause(repo_dir: str, rc: set) -> bool:
    """True if the working tree changed >=1 diagnosed root-cause (SUT) file. A fix that makes the repro pass
    WITHOUT touching the SUT gamed the test (rewrote the test / a fixture / an imported helper) and the bug
    ships unfixed → not honest. Returns True (don't flag) when rc is empty — results lacking root_cause keep
    prior behavior, no false positives. SUBSUMES indirect/data/import test-tamper at the class level."""
    if not rc:
        return True
    return bool(changed_files(repo_dir) & rc)


def harness_dep_digest(repo_dir: str, harness: str, exclude: set) -> str:
    """Content hash of the LOCAL modules the harness DIRECTLY imports, minus the root-cause SUT. A change
    here = the fix neutered an assertion-support file the test imports — an indirect test-tamper the test_dir
    skeptic misses. '' if the harness can't be parsed."""
    base = Path(repo_dir)
    try:
        tree = ast.parse((base / harness).read_text())
    except Exception:
        return ""
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    h = hashlib.sha256()
    for n in sorted(names):
        for rel in (f"{n}.py", f"{n}/__init__.py"):
            if rel in exclude:
                continue
            p = base / rel
            if p.is_file():
                h.update(rel.encode())
                h.update(p.read_bytes())
    return h.hexdigest()


# Stacks whose SUITE has no simple discovery one-liner. Empty is a FIRST-CLASS answer, not a
# fallback: verify.sh's suite_run() already reports "skipped" for an empty --suite-cmd, and the
# repro's own run_command still carries the regression check. What was missing is a detector able
# to SAY empty. pipeline.sh's three-branch version could not: any package.json meant `node --test`
# (wrong for TypeScript and Bun, whose tests are not discovered that way) and EVERYTHING ELSE fell
# through to pytest -- so a Java, Ruby, Rust or PHP repo got a correct diagnosis, a working repro, a
# plausible fix, and then a guaranteed "fix NOT verified" from pytest failing on a repo with no
# Python in it. Ported from the dogfood farm's fix_cell.sh, which had all seven cases right.
_NO_SUITE_RUNNER = ("java", "ruby", "ts", "bun", "unknown")


def detect_stack(worktree: str) -> str:
    """Best-effort stack id for a worktree. Order matters: the narrower signals come first, because
    a Bun or TypeScript project also has a package.json and a Gradle project also has *.java."""
    import os as _os
    j = lambda *p: _os.path.join(worktree, *p)
    ex = _os.path.exists
    if ex(j("go.mod")):                                             return "go"
    if ex(j("bun.lockb")) or ex(j("bun.lock")):                     return "bun"
    if ex(j("package.json")):
        return "ts" if (ex(j("tsconfig.json")) or ex(j("deno.json"))) else "node"
    if ex(j("Gemfile")) or ex(j("Rakefile")):                       return "ruby"
    if ex(j("pom.xml")) or ex(j("build.gradle")) or ex(j("build.gradle.kts")): return "java"
    if ex(j("Cargo.toml")):                                         return "rust"
    if ex(j("pyproject.toml")) or ex(j("setup.py")) or ex(j("setup.cfg")) or ex(j("requirements.txt")):
        return "python"
    return "unknown"


def suite_cmd_for(worktree: str, py: str = "python3") -> str:
    """The repo-suite command, or "" when this stack has no discovery runner worth asserting.

    Returning "" is the point. The previous default of pytest-for-everything did not merely fail to
    run a suite -- it turned a successful fix into a reported FAILURE on every non-Python repo.
    """
    stack = detect_stack(worktree)
    if stack == "go":     return "go test ./..."
    if stack == "node":   return "node --test"
    if stack == "rust":   return "cargo test"
    if stack == "python": return f"{py} -m pytest -q"      # DISCOVERY, not a fixed tests/ dir
    return ""                                              # java · ruby · ts · bun · unknown


def worktree_relative_cmd(cmd: str) -> str:
    """Strip a leading absolute `cd /abs/worktree && ` from a run_command so it is worktree-RELATIVE — the
    verifier already runs it from the worktree root. An absolute path breaks portability and leaks into a
    SECOND worktree (farm finding, 2026-06-20: a Java result emitted `cd /…/wt && javac …`). A relative
    `cd subdir && ` is preserved."""
    return re.sub(r"^cd +/[^&]*&& *", "", cmd or "")


def open_pr(worktree: str, branch: str, title: str, body: str | None = None, *,
            create: bool = True) -> str | None:
    """THE shared PR-creation primitive — one source of truth for the local pipeline, the gha executor,
    and compose-digest (docs: PR-in-run / "local = GHA"). Operates IN a worktree that already has the fix
    applied: branch off it, commit (Steward-attributed via identity.with_trailer — the same Co-Authored-By
    that renders the-steward-bot as contributor), push, and open the PR.

    IDEMPOTENT: if an OPEN PR for `branch` already exists, return its url instead of creating a second one
    — so re-detecting an unfixed signal (same `steward/fix-{sig}` branch) updates ONE PR, never duplicates.
    `create=False` stops after the local commit (no push / no gh) — for dry-run + hermetic selftests.
    FAIL-SOFT: any git/gh error returns None (or '' from gh), never raises — a PR-channel hiccup must not
    break the pipeline. Attribution = the repo's git user as author + the trailer (the proven PR#36 shape)."""
    try:
        from identity import with_trailer
    except Exception:
        def with_trailer(m):  # fail-open: attribution is best-effort, never a hard dep
            return m

    def _g(*a):
        return subprocess.run(["git", "-C", worktree, *a], capture_output=True, text=True)

    try:
        _g("checkout", "-B", branch)
        _g("add", "-A")
        _g("commit", "-m", with_trailer(title[:72]))   # author=git user; trailer=Co-Authored-By: The Steward
        if not create:
            return None
        _g("push", "-f", "-u", "origin", branch)
        # idempotency: reuse an existing OPEN PR for this head rather than creating a duplicate
        ex = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "open",
                             "--json", "url", "-q", ".[0].url"],
                            cwd=worktree, capture_output=True, text=True)
        if (ex.stdout or "").strip():
            return ex.stdout.strip()
        args = ["gh", "pr", "create", "--head", branch]
        args += (["--title", title[:80], "--body", body] if body else ["--fill"])
        r = subprocess.run(args, cwd=worktree, capture_output=True, text=True)
        return (r.stdout or "").strip() if r.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------

def _selftest() -> int:
    f: list[str] = []

    def ck(c, m):
        if not c:
            f.append(m)

    # route derivation — these vectors PIN orchestrator/pipeline.sh:218–225 (drift-guard)
    ck(derive_route(fix_class="config") == "diagnosis-only", "non-mechanical → diagnosis-only")
    ck(derive_route(fix_class="mechanical", reproducibility="worktree-blocked") == "verify-in-prod-patch",
       "blocked mechanical → verify-in-prod-patch")
    ck(derive_route(fix_class="mechanical") == "worktree-reproduced-test", "mechanical → reproduced-test")
    ck(derive_route(route="diagnosis-only", fix_class="mechanical") == "diagnosis-only", "explicit route wins")
    ck(derive_route(route="bogus", fix_class="capacity") == "diagnosis-only", "bad route → derive")

    # no-silencing scan — these vectors PIN orchestrator/verify.sh:silencing_scan (drift-guard)
    ck(silencing_scan("+    except: pass")[0] is False, "except: pass fires")
    ck(silencing_scan("+    try:\n+        risky()\n+    except: pass")[0] is False, "multiline except: pass fires")
    ck(silencing_scan("+    return None")[0] is False, "return None fires")
    ck(silencing_scan("+++ b/x.py\n+    x = compute()")[0] is True, "clean diff passes; +++ ignored")
    ck(silencing_scan("")[0] is True, "empty diff is clean")

    # fix-acceptance rails — shared by gha_executor verify AND verify.sh (drift-guard; F3 unification)
    ck(substantive_diff("+    x = compute()") is True, "real added line is substantive")
    ck(substantive_diff("+    # just a comment\n+\n+++ b/x.py") is False, "comment/blank-only is NOT substantive")
    ck(root_cause_files({"root_cause": {"file": "src/mod.py:5"}}) == {"src/mod.py"}, "root_cause file extracted, line stripped")
    ck(root_cause_files({"diagnosis": {"root_cause": "a.py"}}) == {"a.py"}, "root_cause under diagnosis extracted")
    ck(fix_touched_root_cause(".", set()) is True, "no root_cause → not flagged (preserves prior behavior)")
    ck(worktree_relative_cmd("cd /x/wt && javac A.java") == "javac A.java", "absolute cd stripped → worktree-relative")
    ck(worktree_relative_cmd("cd src && pytest t") == "cd src && pytest t", "relative cd preserved")

    # resolution model — these vectors PIN doc 60 / ledger.py cmd_verify (spec-conformance guard)
    ck(resolution_state(recurred=False, deployed=False, recurred_after_deploy=False, within_settling=False) == "held",
       "no recurrence → held")
    ck(resolution_state(recurred=True, deployed=False, recurred_after_deploy=False, within_settling=False) == "awaiting-deploy",
       "recurred but not deployed → awaiting-deploy")
    ck(resolution_state(recurred=True, deployed=True, recurred_after_deploy=True, within_settling=False) == "persisting",
       "recurred after deploy → persisting (didn't hold)")
    ck(resolution_state(recurred=True, deployed=True, recurred_after_deploy=False, within_settling=True) == "settling",
       "recurred within settling grace → settling")

    # shared PR primitive (PR-in-run) — create=False path is hermetic (commit only, no push/gh)
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _d:
        def _g(*a):
            return subprocess.run(["git", "-C", _d, *a], capture_output=True, text=True)
        _g("init", "-q"); _g("config", "user.email", "t@t"); _g("config", "user.name", "t")
        Path(_d, "a").write_text("1\n"); _g("add", "-A"); _g("commit", "-qm", "base")
        Path(_d, "a").write_text("2\n")                       # an applied "fix" in the worktree
        url = open_pr(_d, "steward/fix-deadbeef", "steward: fix the thing", create=False)
        ck(url is None, "open_pr create=False returns None (no push/gh)")
        on = subprocess.run(["git", "-C", _d, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
        ck(on == "steward/fix-deadbeef", "open_pr created+checked out the branch")
        msg = subprocess.run(["git", "-C", _d, "log", "-1", "--format=%B"],
                             capture_output=True, text=True).stdout
        ck("steward: fix the thing" in msg, "open_pr committed the fix with the title")
        clean = subprocess.run(["git", "-C", _d, "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
        ck(clean == "", "open_pr committed all changes (clean tree after)")

    if f:
        print("steward_core selftest FAILED:")
        for x in f:
            print("  -", x)
        return 1
    print("steward_core selftest OK")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest() if "--selftest" in sys.argv else 0)
