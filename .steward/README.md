# .steward/ — GHA-mode Steward runner (vendored)

These files are **vendored copies** of the Steward's gha-mode tools from the
`the-steward` repo (`tools/state_branch.py`, `tools/gha_signal.py`,
`tools/gha_executor.py`), plus thin glue (`watch.py`, `execute.py`). They let
this repo run the Steward loop in its own GitHub Actions with no cross-repo
checkout and no extra infra.

- `state_branch.py` — read/write cursor + handled + ledger + results over the
  `steward-state` branch (single-writer; `*.jsonl` union-merge).
- `gha_signal.py` — run-log → deduplicated error signatures.
- `gha_executor.py` — **investigate-first** executor (native `claude`, no `ir`): runs the
  investigate contract → commits to a `route` → `diagnosis-only` (default) writes a diagnosis
  with NO PR; mechanical/verify-in-prod routes reach fix+verify+PR. Mirrors `orchestrator/pipeline.sh`.
- `contracts/` — vendored agent contracts (`investigate.md`, `fix.md` from `agents/*/agent.md`).
- `watch.py` / `execute.py` — the workflow glue (`.github/workflows/steward.yml`).

**Auth:** the executor drives `claude` headless on a **subscription** via the
`CLAUDE_CODE_OAUTH_TOKEN` secret (no API key). `ANTHROPIC_API_KEY`, or economy
(`+ ANTHROPIC_BASE_URL` → inferroute), also work — selected purely by env.

**Vendored, not packaged (Phase 1):** updates to the upstream tools must be
re-copied here. Productionizing = publish the runner as an installable package /
a reusable workflow so repos don't carry copies (tracked in the-steward docs/43).
