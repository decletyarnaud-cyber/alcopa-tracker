#!/usr/bin/env python3
"""Run-log signal adapter for GHA mode (docs/62 P1.4).

The simplest gha-mode signal: a batch job (e.g. models_arb's daily run) *is* the
watcher workflow, and its own run output is the signal. This adapter turns raw
run-log text into deduplicated error *signatures* the watcher can act on:

  - detect error-class lines (Traceback / "Failed …" / "… error" / timeouts);
  - normalise each into a STABLE signature by masking volatile tokens (char/line/
    column positions, counts, hex, timestamps, hex-ish ids) so the *same* bug
    recurring produces the *same* signature — that's what makes dedup work;
  - collapse repeats within a run, then drop any signature already in the
    handled-list (from the steward-state branch).

It is the run-log analogue of the get-logs `severity_patterns` + masking shape,
scoped to the "run-it-as-a-workflow, failures are the signal" model. Pure: it
takes log text + a handled set, and returns novel signals. The watcher (P1.5)
wires it to StateBranch (handled / cursor) and to repository_dispatch.
"""
from __future__ import annotations

import hashlib
import re

# Error-class line detection (case-insensitive, word-ish boundaries).
_ERROR_RE = re.compile(
    r"\b(error|errors|exception|failed|failure|fatal|critical|traceback|"
    r"could not|cannot|unable to|timed out|timeout|refused|denied|"
    # keyword-LESS crash dialects (Go/Rust/native/k8s) — a panic or segfault carries no "error"
    # word, so without these the detector misses the crash entirely (the assumption-(a) gap).
    r"panic|panicked|segfault|segmentation fault|sigsegv|sigabrt|core dumped|oomkilled|out of memory)\b",
    re.IGNORECASE,
)
_HIGH_RE = re.compile(
    r"\b(fatal|critical|traceback|panic|panicked|segfault|sigsegv|sigabrt|oomkilled)\b", re.IGNORECASE)

# A results-tally line ("Done: 7 matched, 153 no-match, 20 errors") trips _ERROR_RE on the word "errors"
# but is a SUMMARY, not a defect. Skip lines carrying ≥2 "N <count-word>" tallies (C12 — these were spawning
# a wasted executor job per run).
_SUMMARY_RE = re.compile(
    r"(?:\b\d[\d,]*\s+(?:matched|no-?match|unmatched|errors?|resolved|unresolved|skipped|processed|"
    r"models|new|remaining)\b[^\n]*){2,}", re.IGNORECASE)

# Leading log timestamp prefixes (journald-ish / ISO) to strip before masking.
_TS_PREFIX = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*|"
                        r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+")

# Volatile-token masks applied in order (specific → generic). The id/secret masks mirror the getlogs
# built-in canonicalizations (docs/85 §5.1): a scrubber FINGERPRINT (`<SECRET:generic:01599d>`, a
# different hash per request) or a raw `req_…` correlation id would otherwise mint a fresh signature
# every request — the proven 1,334→~24 request-id churn. Collapse them BEFORE the generic hex/number
# masks so the id class is stable regardless of which producer (get-logs or gha run-log) minted it.
_MASKS = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<TS>"),
    (re.compile(r"<SECRET(?::[^>]*)?>"), "<SECRET>"),           # per-secret scrubber fingerprint → one token
    (re.compile(r"\breq_[0-9A-Za-z]{6,}\b"), "<SECRET>"),       # raw request/correlation id
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<HEX>"),
    (re.compile(r"\[<HEX>\]"), "[<SECRET>]"),                   # a bracketed correlation id == the scrubbed [<SECRET>]
    (re.compile(r"/[^\s'\"]+"), "<PATH>"),
    (re.compile(r"\d+"), "<N>"),
)


# ANSI SGR colour codes. A coloured stderr (loguru, werkzeug, pytest, npm…) otherwise carries escape
# bytes straight into the signature, the job name and the model's prompt — and worse, makes the SAME
# failure hash differently depending on whether it was captured from a TTY, so switching signal source
# re-emits every already-handled bug as novel.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Python names its exception classes in CamelCase with an `Error`/`Exception` suffix, which
# `\berror\b` can never match: in `KeyError` the word boundary before "Error" does not exist. Without
# this, 7 of 8 common exception lines are invisible to the detector and the ONLY line of a crash that
# registers is the `Traceback (most recent call last):` header — identical for every crash in every
# repo, so all crashes collapse to one signature and the watch goes deaf after the first.
# Case-SENSITIVE by design: lowercase "error"/"exception" is already covered above.
_EXC_RE = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b")

# An error word hyphen-joined on BOTH sides is part of a slug or identifier, not a message —
# observed live: a vehicle listing `…-fap-exception-euro-5-2011-…` was spawning executor jobs.
_SLUG_EMBEDDED = re.compile(
    r"-(?:error|errors|exception|failed|failure|fatal|critical|timeout)-", re.IGNORECASE)

_TB_HEADER = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")


def strip_ansi(text: str) -> str:
    """PURE."""
    return _ANSI.sub("", text or "")


def fold_tracebacks(text: str) -> str:
    """PURE: collapse each Python traceback block to its FINAL line — the exception itself.

    A traceback's identity is `KeyError: 'mise_a_prix'`, not the header every traceback shares, and
    not the intervening frames (which carry line numbers that shift on every edit). Folding gives one
    signal per crash, keyed on the thing that actually distinguishes one crash from another."""
    out, lines, i = [], (text or "").splitlines(), 0
    while i < len(lines):
        if not _TB_HEADER.match(lines[i]):
            out.append(lines[i]); i += 1
            continue
        j = i + 1
        while j < len(lines) and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
            j += 1
        if j < len(lines) and lines[j].strip():
            out.append(lines[j].strip())        # the exception line IS the signal
        else:
            out.append(lines[i].strip())        # truncated trace — keep the header, better than nothing
        i = j + 1 if j < len(lines) else j
    return "\n".join(out)


def is_error_line(line: str) -> bool:
    """PURE: does this line report a defect?"""
    if _SUMMARY_RE.search(line):
        return False
    if _SLUG_EMBEDDED.search(line) and not _EXC_RE.search(line):
        return False
    return bool(_ERROR_RE.search(line) or _EXC_RE.search(line))


def signature_text(line: str) -> str:
    """Canonical, volatility-masked form of a log line."""
    s = _TS_PREFIX.sub("", strip_ansi(line).strip())
    for rx, repl in _MASKS:
        s = rx.sub(repl, s)
    return re.sub(r"\s+", " ", s).strip()


def sig_id(line: str) -> str:
    return hashlib.sha1(signature_text(line).encode()).hexdigest()[:12]


def _severity(line: str) -> str:
    return "high" if _HIGH_RE.search(line) else "error"


def extract(log_text: str) -> list[dict]:
    """All error-class signals in a run log, collapsed by signature.

    Returns dicts: {sig, signature, severity, count, sample} ordered by first
    appearance."""
    seen: dict[str, dict] = {}
    for raw in fold_tracebacks(strip_ansi(log_text)).splitlines():
        line = raw.rstrip()
        if not line or not is_error_line(line):
            continue
        sig = sig_id(line)
        if sig in seen:
            seen[sig]["count"] += 1
            continue
        seen[sig] = {
            "sig": sig,
            "signature": signature_text(line),
            "severity": _severity(line),
            "count": 1,
            "sample": line.strip()[:300],
        }
    return list(seen.values())


def novel(log_text: str, handled: set) -> list[dict]:
    """Error signals from the run log whose signature is not already handled."""
    return [s for s in extract(log_text) if s["sig"] not in (handled or set())]


# ---------------------------------------------------------------------------

_SAMPLE_RUN = """\
[nebius] 0 models | no changes
[cerebras] 2 models | no changes
[parasail] 22 models | no changes
[llm_matcher] Invalidated 175 stale no-match entries
[llm_matcher] 180 new models to process
[llm_matcher] Failed to parse response: Expecting ',' delimiter: line 13 column 19 (char 2340)
[llm_matcher] Done: 7 matched, 153 no-match, 3 researched, 20 errors
[cross_provider] Layer 1 (algo): 0 resolved, 131 remaining
[notify] Sent alert: novita/xiaomimimo/mimo-v2-flash
"""


def _selftest() -> int:
    failures: list[str] = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # 1) a run with the parse error yields >=1 signal incl. the parse-fail
    sigs = extract(_SAMPLE_RUN)
    parse = [s for s in sigs if "Failed to parse response" in s["signature"]]
    check(len(parse) == 1, "exactly one parse-fail signal")
    check(parse and parse[0]["severity"] == "error", "parse-fail is error severity")

    # C12: a results-tally SUMMARY ('Done: N matched, N no-match, N errors') is NOT a signal,
    # but a real error carrying a single count still is.
    check(extract("[llm_matcher] Done: 7 matched, 153 no-match, 20 errors") == [], "summary tally is not a signal")
    check(len(extract("[llm_matcher] ERROR: rate limit (429), 47 models unmatched")) == 1,
          "a real error with one count is still a signal")

    # crash dialects that carry NO error keyword must still be detected (assumption-(a) gap — the
    # diverse-canary direction: Go/Rust/native/k8s crashes don't say "error").
    check(len(extract("panic: assignment to entry in nil map")) == 1, "Go panic detected (no error keyword)")
    check(extract("panic: assignment to entry in nil map")[0]["severity"] == "high", "panic is high severity")
    check(len(extract("thread 'main' panicked at 'index out of bounds: the len is 3'")) == 1, "Rust panic detected")
    check(len(extract("Segmentation fault (core dumped)")) == 1, "segfault detected")
    check(len(extract("container terminated reason OOMKilled")) == 1, "OOMKilled detected (no other keyword)")
    check(extract("nightly summary: 0 panics, all green") == [], "'panics' (plural noun) is not a false positive")

    # 2) novelty: first pass non-empty; after handling all, repeat emits none
    handled: set = set()
    n1 = novel(_SAMPLE_RUN, handled)
    check(len(n1) >= 1, "first pass emits novel signals")
    check(any("Failed to parse" in s["signature"] for s in n1), "parse-fail is novel first time")
    handled |= {s["sig"] for s in n1}
    n2 = novel(_SAMPLE_RUN, handled)
    check(n2 == [], "repeat run emits no novel signals (dedup works)")

    # 3) normalisation: same error, different volatile positions → same signature
    a = "[llm_matcher] Failed to parse response: Expecting ',' delimiter: line 13 column 19 (char 2340)"
    b = "[llm_matcher] Failed to parse response: Expecting ',' delimiter: line 7 column 4 (char 991)"
    check(sig_id(a) == sig_id(b), "volatile positions masked → identical signature")

    # 4) genuinely different errors → different signatures
    c = "[provider] API error: 503 Service Unavailable"
    check(sig_id(a) != sig_id(c), "distinct errors → distinct signatures")

    # 4b) id/secret normalization (docs/85 §5.1): a scrubber FINGERPRINT and a raw req_ correlation id
    # each vary per request — they must NOT fragment one shape into a fresh signature per request.
    s1 = "[messages] [<SECRET:generic:01599d>] Backend error from chutes: 500"
    s2 = "[messages] [<SECRET:generic:0a1eb3>] Backend error from chutes: 500"
    check(sig_id(s1) == sig_id(s2), "per-secret scrubber fingerprint collapses to one signature")
    check("<SECRET>" in signature_text(s1) and "generic" not in signature_text(s1), "fingerprint → <SECRET>")
    r1 = "[messages] [req_078fefa882454dad91a126a5] Backend error from chutes: 500"
    check(sig_id(r1) == sig_id(s1), "a raw req_ id collapses to the same [<SECRET>] as the fingerprint")

    # 5) a GitHub-Actions ISO timestamp prefix doesn't fragment the signature
    #    (GHA prefixes every run-log line with an ISO 8601 timestamp)
    d = "2026-06-18T12:51:15.1607414Z " + a
    check(sig_id(d) == sig_id(a), "ISO/GHA timestamp prefix stripped before signature")

    # 6) non-error lines are ignored
    check(extract("[nebius] 0 models | no changes\n[ok] all good") == [], "no false-positive signals")

    # 7) Python exception lines are DETECTED. `\berror\b` cannot match CamelCase `…Error`, so before
    #    this the only part of a crash that registered was the shared traceback header.
    for exc in ("ZeroDivisionError: division by zero",
                "FileNotFoundError: [Errno 2] No such file or directory",
                "KeyError: 'mise_a_prix'",
                "AttributeError: 'NoneType' object has no attribute 'text'",
                "TypeError: unsupported operand type(s)",
                "sqlite3.OperationalError: database is locked",
                "requests.exceptions.ConnectionError: HTTPSConnectionPool"):
        check(is_error_line(exc), f"exception line detected: {exc[:34]}")

    # 8) a traceback folds to its EXCEPTION line, so two unrelated crashes are two signals — not one
    #    shared `Traceback (most recent call last):` that mutes every future crash once settled.
    tb = lambda e: f"Traceback (most recent call last):\n  File \"x.py\", line 3, in f\n    boom()\n{e}\n"
    s_a = extract(tb("KeyError: 'mise_a_prix'"))
    s_b = extract(tb("sqlite3.OperationalError: database is locked"))
    check(len(s_a) == 1 and "KeyError" in s_a[0]["signature"], "traceback keyed on its exception line")
    check(s_a[0]["sig"] != s_b[0]["sig"], "two unrelated crashes are two distinct signatures")
    check(bool(novel(tb("sqlite3.OperationalError: database is locked"), {s_a[0]["sig"]})),
          "a new crash is still novel after a DIFFERENT crash was handled")
    check("Traceback (most recent" not in s_a[0]["signature"], "the shared header is not the signature")
    # a truncated trace (no exception line) still yields something rather than vanishing
    check(len(extract("Traceback (most recent call last):\n  File \"x.py\", line 3, in f\n")) == 1,
          "a truncated traceback still emits one signal")

    # 9) ANSI: the same failure hashes identically whether or not the sink was a TTY, so switching
    #    signal source (ci-run <-> shipped log) does not re-emit everything as novel.
    esc = chr(27)
    coloured = (f"{esc}[32m2026-08-18 18:45:01{esc}[0m | {esc}[33mWARNING{esc}[0m | "
                "base_scraper - Request failed: timed out")
    plain = "2026-08-18 18:45:01 | WARNING | base_scraper - Request failed: timed out"
    check(sig_id(coloured) == sig_id(plain), "ANSI-coloured and plain lines share one signature")
    check(esc not in extract(coloured)[0]["signature"], "no escape bytes leak into the signature")
    check(esc not in extract(coloured)[0]["sample"], "no escape bytes leak into the model's sample")

    # 10) an error word hyphen-joined inside a slug is an identifier, not a defect (observed live:
    #     a vehicle listing `…-fap-exception-euro-5-…` was spawning one executor job per run)
    check(not is_error_line(
        "Fetching 1887/1926: scenic-iii-dci-130-fap-exception-euro-5-2011-1101964   [CT] PDF"),
        "an error word embedded in a slug is not a signal")
    check(is_error_line("ValueError: could not convert string to float"),
          "...but a real exception in a line with hyphens still counts")

    if failures:
        print("gha_signal selftest FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("gha_signal selftest OK")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    # CLI: pipe a run log in, get novel signals (handled-list via --handled f.jsonl)
    import json
    handled = set()
    if "--handled" in sys.argv:
        hp = sys.argv[sys.argv.index("--handled") + 1]
        try:
            for ln in open(hp):
                ln = ln.strip()
                if ln:
                    handled.add(json.loads(ln).get("sig"))
        except Exception:
            pass
    print(json.dumps(novel(sys.stdin.read(), handled), indent=2))
