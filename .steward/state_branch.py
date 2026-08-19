#!/usr/bin/env python3
"""State-branch organ backend for GHA mode (doc 61 §3 / docs/62 P1.2).

In GHA mode the authoritative Steward state for a repo lives in a `steward-state`
branch of that repo (no always-on box). This module is the read/write backend
over such a branch:

  cursor.json   — the signal-fetch cursor (watcher-owned)
  handled.jsonl — append-only set of handled signal signatures (watcher-owned)
  ledger.jsonl  — append-only observations/threads (git-mergeable port of the
                  local SQLite ledger; watcher-owned)
  results/<id>.json — one file per executor run (executor-owned, distinct paths)

**Single-writer discipline:** the watcher owns cursor/handled/ledger and is the
only writer of those; executors only ever create `results/<signal-id>.json` at
distinct paths, so concurrent executor writes never touch the same file. Pushes
use fetch+rebase-retry so a race widens into a retry, never a lost write.

It operates on a *dedicated* checkout dir (e.g. the gha_store cache, or a fresh
runner checkout) — never the user's working tree — so it never disturbs a normal
clone's branch. `--selftest` exercises the full round-trip against a local bare
repo standing in for the GitHub remote.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
# Capture-time OWNER stamp (T-1 / SA-24). This is the SECOND decisions.jsonl store — the one that
# lives on the GHA state branch, inside the tenant's own repo — and its rows are read back into our
# decision logic and mirrored (best-effort) into the machine store, so an unstamped row here arrives
# in our plane with no owner. Imported fail-open: a stripped install degrades to unstamped rows
# (which read UNSET — the honest answer), never to a lost decision.
try:
    import tenant as _tenant  # noqa: E402
except Exception:                                        # pragma: no cover — defensive
    _tenant = None

DEFAULT_BRANCH = "steward-state"
SEED_FILES = ("cursor.json", "handled.jsonl", "ledger.jsonl")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class StateBranchError(RuntimeError):
    pass


class StateBranch:
    def __init__(self, work_dir: str, remote_url: str | None = None,
                 branch: str = DEFAULT_BRANCH, remote: str = "origin"):
        self.dir = Path(os.path.expanduser(work_dir))
        self.remote_url = remote_url
        self.branch = branch
        self.remote = remote

    # --- git plumbing -------------------------------------------------------

    def _git(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", str(self.dir), *args],
                           capture_output=capture, text=True)
        if check and r.returncode != 0:
            raise StateBranchError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return r

    def _remote_has_branch(self) -> bool:
        r = self._git("ls-remote", "--heads", self.remote, self.branch, check=False)
        return bool(r.stdout.strip())

    # --- lifecycle ----------------------------------------------------------

    def ensure(self) -> "StateBranch":
        """Idempotently make `self.dir` a checkout of the state branch, creating
        and seeding it (orphan) on the remote the first time."""
        if not (self.dir / ".git").is_dir():
            self.dir.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q")
            if self.remote_url:
                self._git("remote", "add", self.remote, self.remote_url)
        # make sure identity exists for commits in ephemeral checkouts
        if not self._git("config", "user.email", check=False).stdout.strip():
            self._git("config", "user.email", "steward@local")
            self._git("config", "user.name", "the-steward")

        if self.remote_url:
            # Reconcile the remote URL every ensure(). `remote add` only runs on fresh init, so a store
            # created by an EARLIER sync with a stale url (a first attempt used git@github.com/SSH and
            # failed; a later call passes HTTPS) would keep the dead remote and every fetch keep failing.
            # set-url fixes an existing store; add covers a missing origin. [ade 2026-08-19]
            if self._git("remote", "set-url", self.remote, self.remote_url, check=False).returncode != 0:
                self._git("remote", "add", self.remote, self.remote_url, check=False)
            self._git("fetch", "-q", self.remote, check=False)

        if self.remote_url and self._remote_has_branch():
            self._git("checkout", "-q", "-B", self.branch, f"{self.remote}/{self.branch}")
        else:
            # create the orphan branch + seed it
            cur = self._git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
            if cur != self.branch:
                self._git("checkout", "-q", "--orphan", self.branch)
            self._git("rm", "-rf", "--cached", ".", check=False)
            self._seed()
            self._git("add", "-A")
            self._git("commit", "-q", "-m", "seed steward-state")
            if self.remote_url:
                try:
                    self._git("push", "-q", "-u", self.remote, self.branch)
                except StateBranchError:
                    # A create-push has an AMBIGUOUS failure mode, observed live: git's HTTP
                    # transport retried after a transient error, the FIRST attempt had already
                    # created the ref, and the retry came back "cannot lock ref … reference already
                    # exists" — so the push both succeeded (remote has our seed) and reported
                    # failure. The same shape covers a genuine race with another first writer.
                    # Either way the branch now existing IS the goal state: adopt the remote's copy
                    # (ours and theirs are equivalent empty seeds) instead of failing the bootstrap.
                    self._git("fetch", "-q", self.remote, check=False)
                    if not self._remote_has_branch():
                        raise                       # push genuinely failed — surface it
                    self._git("checkout", "-q", "-B", self.branch, f"{self.remote}/{self.branch}")
        return self

    def _seed(self) -> None:
        # Append-only logs auto-union on merge/rebase so concurrent appends never
        # conflict (results use distinct paths and need no merge driver). This is
        # the safety net under the watcher-is-single-writer discipline.
        ga = self.dir / ".gitattributes"
        if not ga.exists():
            ga.write_text("*.jsonl merge=union\n")
        if not (self.dir / "cursor.json").exists():
            (self.dir / "cursor.json").write_text(json.dumps({"cursor": None, "updated_at": _now()}) + "\n")
        for f in ("handled.jsonl", "ledger.jsonl"):
            (self.dir / f).touch()
        results = self.dir / "results"
        results.mkdir(exist_ok=True)
        (results / ".keep").touch()

    def pull(self) -> None:
        if self.remote_url:
            self._git("fetch", "-q", self.remote, check=False)
            self._git("reset", "-q", "--hard", f"{self.remote}/{self.branch}", check=False)

    def commit_push(self, message: str, retries: int = 3) -> bool:
        """Commit any working changes and push, rebasing onto the remote on
        rejection (single-writer-safe). Returns True if something was pushed,
        False if there was nothing to commit."""
        self._git("add", "-A")
        if not self._git("diff", "--cached", "--quiet", check=False).returncode:
            return False  # nothing staged
        self._git("commit", "-q", "-m", message)
        if not self.remote_url:
            return True
        for attempt in range(retries + 1):
            r = self._git("push", "-q", self.remote, self.branch, check=False)
            if r.returncode == 0:
                return True
            # rejected (someone else wrote): rebase onto remote and retry
            self._git("fetch", "-q", self.remote, check=False)
            rb = self._git("rebase", f"{self.remote}/{self.branch}", check=False)
            if rb.returncode != 0:
                self._git("rebase", "--abort", check=False)
                raise StateBranchError("state-branch rebase conflict (unexpected with distinct paths)")
        raise StateBranchError("state-branch push failed after retries")

    # --- typed state accessors ---------------------------------------------

    def _read_json(self, name: str, default):
        try:
            return json.loads((self.dir / name).read_text())
        except Exception:
            return default

    def _read_jsonl(self, name: str) -> list[dict]:
        out = []
        try:
            for line in (self.dir / name).read_text().splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except Exception:
            pass
        return out

    def get_cursor(self):
        return self._read_json("cursor.json", {"cursor": None}).get("cursor")

    def set_cursor(self, value) -> None:
        (self.dir / "cursor.json").write_text(json.dumps({"cursor": value, "updated_at": _now()}) + "\n")

    def handled(self) -> set:
        return {r.get("sig") for r in self._read_jsonl("handled.jsonl") if r.get("sig")}

    def handled_counts(self) -> dict:
        """sig -> number of times it was dispatched (handled.jsonl is an append-only attempt log).
        The watcher uses this to retry a non-terminal signal up to a cap (see gha_executor.settled_sigs)."""
        counts: dict = {}
        for r in self._read_jsonl("handled.jsonl"):
            sig = r.get("sig")
            if sig:
                counts[sig] = counts.get(sig, 0) + 1
        return counts

    def add_handled(self, sig: str) -> None:
        with (self.dir / "handled.jsonl").open("a") as f:
            f.write(json.dumps({"sig": sig, "at": _now()}) + "\n")

    def decisions(self) -> dict:
        """sig -> the latest human decision (merged/rejected/deferred) from decisions.jsonl.
        This is the morning/local surface recording its call BACK to the branch (doc 61 §3)."""
        out: dict = {}
        for r in self._read_jsonl("decisions.jsonl"):
            if r.get("sig"):
                out[r["sig"]] = r
        return out

    def record_decision(self, sig: str, decision: str, **meta) -> None:
        """Append a human decision to the state branch's own decisions.jsonl.

        The `tenant` stamp is T-1's rule at this second write point (SA-24): the machine store is
        NOT a superset of this one — `gha_sync` mirrors branch → machine best-effort, so a mirror
        hiccup leaves a decision that exists only here. Absence writes `UNSET` (unpoolable by
        definition) rather than omitting the key, and a caller that set `tenant` deliberately is
        never overwritten. Fail-open: a resolver that cannot answer costs the label, never the row.

        The id itself is the operator's explicit, opaque label (`tenant.py` rule 1) — this file is
        written into the tenant's own git history permanently, so nothing about the pool (enrolment
        order, cardinality) may be derived into it here."""
        row = {"sig": sig, "decision": decision, "at": _now(), **meta}
        if _tenant is not None and "tenant" not in row:
            try:
                _tenant.stamp(row)
            except Exception:                            # pragma: no cover — stamp is itself fail-open
                pass
        with (self.dir / "decisions.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")

    def ledger(self) -> list[dict]:
        return self._read_jsonl("ledger.jsonl")

    def append_ledger(self, record: dict) -> None:
        with (self.dir / "ledger.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    def write_result(self, signal_id: str, data: dict) -> Path:
        d = self.dir / "results"
        d.mkdir(exist_ok=True)
        # distinct path per signal → executors never collide
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in signal_id)
        p = d / f"{safe}.json"
        p.write_text(json.dumps(data, indent=2) + "\n")
        return p

    def results(self) -> dict[str, dict]:
        d = self.dir / "results"
        out = {}
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                try:
                    out[p.stem] = json.loads(p.read_text())
                except Exception:
                    pass
        return out


# ---------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile

    failures: list[str] = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        origin = os.path.join(td, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)

        # writer A: first-time setup creates + seeds the orphan branch, then writes
        a = StateBranch(os.path.join(td, "wa"), remote_url=origin).ensure()
        check(a.get_cursor() is None, "seeded cursor is None")
        check(a.handled() == set(), "seeded handled empty")
        a.set_cursor("2026-06-18T00:00:00Z")
        a.add_handled("sig-aaa")
        a.append_ledger({"id": "t1", "signature": "parse-fail", "status": "open"})
        a.write_result("job-1", {"verdict": "verified", "pr": 7})
        pushed = a.commit_push("watcher tick 1")
        check(pushed is True, "first write pushes")

        # ambiguous create-push (observed live in CI): the push CREATED the remote ref and then
        # reported failure — git's transport retried after a transient, the first attempt had
        # landed, the retry got "reference already exists". ensure() must adopt the now-existing
        # remote branch instead of failing the bootstrap. Simulated: the branch-existence probe
        # says NO once (forcing the orphan path), the push "fails" while actually landing, and the
        # re-probe tells the truth.
        amb = StateBranch(os.path.join(td, "amb"), remote_url=origin)
        probes = {"n": 0}
        real_probe, real_git = amb._remote_has_branch, amb._git
        amb._remote_has_branch = lambda: (probes.__setitem__("n", probes["n"] + 1)
                                          or probes["n"] > 1) and real_probe()
        def flaky_push(*args, **kw):
            r = real_git(*args, **kw)
            if args and args[0] == "push":
                raise StateBranchError("cannot lock ref: reference already exists (simulated retry)")
            return r
        amb._git = flaky_push
        try:
            amb.ensure()
        except StateBranchError:
            failures.append("ensure() must adopt the branch after an ambiguous create-push")
        amb._git = real_git
        amb._remote_has_branch = real_probe
        check(probes["n"] >= 2, "recovery re-probed the remote instead of trusting the error")
        check(amb.get_cursor() == "2026-06-18T00:00:00Z",
              "the adopted checkout carries the REMOTE's state, not the discarded local seed")

        # reader B: a FRESH clone must see everything (round-trip via the remote)
        b = StateBranch(os.path.join(td, "wb"), remote_url=origin).ensure()
        check(b.get_cursor() == "2026-06-18T00:00:00Z", "fresh clone reads cursor")
        check("sig-aaa" in b.handled(), "fresh clone reads handled")
        check(any(r.get("id") == "t1" for r in b.ledger()), "fresh clone reads ledger")
        check(b.results().get("job-1", {}).get("pr") == 7, "fresh clone reads result")

        # single-writer race: B writes a distinct result + handled, pushes;
        # A then writes too and must rebase cleanly (distinct paths, no conflict)
        b.add_handled("sig-bbb")
        b.write_result("job-2", {"verdict": "diagnosis"})
        check(b.commit_push("executor job-2") is True, "B pushes")
        a.add_handled("sig-ccc")
        a.write_result("job-3", {"verdict": "verified"})
        check(a.commit_push("executor job-3") is True, "A pushes after rebase")

        # final fresh clone sees the union
        c = StateBranch(os.path.join(td, "wc"), remote_url=origin).ensure()
        check({"sig-aaa", "sig-bbb", "sig-ccc"} <= c.handled(), "union of handled survives concurrent writers")
        check({"job-1", "job-2", "job-3"} <= set(c.results().keys()), "all results survive")
        before = c.handled_counts().get("sig-aaa", 0)
        c.add_handled("sig-aaa"); c.add_handled("sig-aaa")
        check(c.handled_counts().get("sig-aaa") == before + 2, "handled_counts increments per dispatch")
        c.record_decision("sig-bbb", "merged", pr="http://pr/2")
        check(c.decisions().get("sig-bbb", {}).get("decision") == "merged", "record_decision round-trips via decisions.jsonl")

        # idempotent ensure() on an existing checkout
        a.ensure()
        check(a.get_cursor() == "2026-06-18T00:00:00Z", "ensure() idempotent")

    if failures:
        print("state_branch selftest FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("state_branch selftest OK")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("usage: state_branch.py --selftest")
