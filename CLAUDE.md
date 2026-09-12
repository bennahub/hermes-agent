<!-- Maintained source: AGENTS.md in this directory. This file mirrors it verbatim below.
     Claude Code resolves an `@import` against the session working directory, so a root
     CLAUDE.md containing only `@AGENTS.md` loads nothing when the session starts in a
     subdirectory. Keeping the text here makes these instructions load from any cwd.
     Edit AGENTS.md and copy it here; the two must stay byte-identical below this header. -->

# Hermes — repository instructions

## Scope and architecture

- This is the Hermes source repository. A development checkout, an installed runtime, and an immutable deployed release are different targets; identify the one relevant to the task. These instructions do not authorize deployment, service restarts, live-data repair, or changes to other products.
- Preserve the narrow core agent/model tool schema. Prefer an existing capability; then a native CLI plus skill, configured service-gated tool, plugin, or existing MCP integration. Add a core tool only for a demonstrated need those surfaces cannot serve. This is an integration policy, not a prohibition on new product features.
- Extend real interfaces rather than adding speculative hooks, managers, or orchestration. Profiles are intentionally isolated; an existing clone operation is not a reason to introduce live inheritance.
- Behavioral configuration belongs in `config.yaml`; secrets belong in the credential/environment mechanisms. Resolve paths with `get_hermes_home()` and display paths with `display_hermes_home()`. Profile-management roots are intentionally home-anchored; do not confuse them with the selected profile.
- Preserve conversation contracts: prompt cache stability, the conversation's system-prompt stability, valid message-role sequencing, and no synthetic mid-loop user command. Compaction is the supported context-change exception; settings/skills/tools changes use the existing cache-aware deferred-next-session behavior and explicit opt-in immediate path. Changes to compaction, replay, recovery, or tool execution must retain completed-action and authority boundaries.
- Instructional tools must deliver the complete required skill/playbook once invoked. Do not introduce offset/limit escape hatches that silently omit required content. This does not require preloading unrelated skills or reference documents.
- Surface capabilities are session-scoped. Desktop/GUI availability follows the session/platform toolset, not just a backend environment flag. `check_fn` tests reachability/opt-in and may be cached process-wide; do not store per-session eligibility there.
- Preserve data-path authorization and opt-in boundaries. Do not add telemetry or third-party usage attribution without the approved user-facing opt-in. Third-party service plugins belong in standalone plugin distributions rather than special cases in core.

## Navigating and changing code

- Large entry points are facades with topical siblings: `agent/turn_*.py`, `hermes_state_*.py`, `gateway/run_*.py`, `hermes_cli/cli_*_mixin.py`, and similar families. Find the actual implementation and call-site binding; preserve public entry points without appending unrelated logic to a facade.
- Avoid module-level circular imports. Internal code uses the defining implementation rather than external compatibility shims. Read the current compat manifests when relevant; do not assume a removal date remembered from an old release still applies.
- When moving symbols, update affected documentation and instruction references; preserve the compatibility contract for public/extension-facing behavior. Structural limits enforced by current CI still apply; do not force a separate refactor just to satisfy an arbitrary prose threshold.
- Use the existing process-identity matchers, not substring tests on argv. Derive flags from the parser. Argparse aliases may preserve the literal alias in `dest`, so dispatch must handle supported aliases.
- In TypeScript UI code, follow the existing feature-local shared stores, thin route shells, and colocated actions; avoid duplicating an existing store or controller. The applicable area guide owns details.
- Respect current dependency bounds and lockfiles: upper-bounded Python ranges; full commit pins for Git dependencies/Actions; exact CI-only pip pins. The documented pre-1.0 bound convention is `<0.(minor+2)`. Update the lockfile when its manifest changes; obtain versions from current manifests, not this file.

## Validation that protects real user state

- Run Python tests through `scripts/run_tests.sh`, not bare `pytest`: the wrapper provides credential isolation, UTC/locale settings, a temporary `HERMES_HOME`, and per-file process isolation. Select the affected files/directories and required CI scope; the wrapper's full-suite mode is not mandatory for every edit.
- Examples: `scripts/run_tests.sh tests/gateway/` or `scripts/run_tests.sh <affected-test-file> -k <case>`. For JS/TS packages use their current package scripts and CI classification instead of routing all tests through Python.
- Tests never write to a user's real Hermes home. Preserve `_isolate_hermes_home`; profile tests isolate both `Path.home()` and `HERMES_HOME`. Use the project interpreter and required plugins. A baseline failure in a broken environment is not proof that a defect predates the candidate.
- Choose enough behavioral/invariant coverage to establish the changed contract. There is no fixed one-or-two-test quota. Resolution, config propagation, security boundaries, remote backends, and file/network I/O need relevant real-path integration evidence in isolated resources; mocks alone can miss the wiring.
- Prefer behavioral contracts over source-string assertions or mutable catalog/count snapshots. Keep legitimate integrity, dependency, and declarative packaging checks: these are not substitutes for behavior tests, but byte identity can itself be a real contract.
- Host-dependent behavior is tested on the actual host with the repository's named `linux_only`, `macos_only`, or `windows_only` markers. Do not fake the interpreter OS or replace the markers with aliases/skip rules the CI collector cannot discover. Pure functions may take a platform as data.
- A Python test for JS-only source/manifest changes may be excluded by change classification. Put such checks in the appropriate package suite. Use the current Windows process-topology lane when that subsystem is changed; read the workflow rather than assuming every push runs it.
- Report the exact scope and result. A retry that passes is still a recorded flaky run; a subset is not the full suite. Follow current runner settings rather than adding independent retry loops.

## Git, review, and release

- Preserve unrelated and concurrent work, contributors' authorship, and existing release history. Do not use `reset --hard`, an unverified stash/pop pair, or a bulk restore as a default reconciliation step.
- Before an authorized merge, compare the actual base, candidate, and current target for integration risk. Reconcile in an isolated task worktree with a non-destructive Git method; review the resulting delta and run the affected checks. Do not chase unrelated main movement during ordinary development.
- Use the current repository contribution/release procedures for the requested action. If the installed `hermes-agent-dev` skill governs a PR/salvage task, inspect it then; its absence is not a blocker for unrelated coding or a reason to invent a replacement workflow.
- Deployed/reviewed identity requires applicable artifact/content evidence, not merely a directory name, cached `.bytecode-fingerprint`, stale Git metadata, or an executor's assertion. Verify the live target and relevant manifest before making the claim.
- A task's existing authorization covers its routine in-scope mechanics. A new product/architecture decision, missing permission, destructive action outside scope, or unauthorized Production/external effect needs resolution; do not turn a permission refusal into a workaround.

## Area guides — load only those relevant to the change

| Work area | Guide |
| --- | --- |
| `run_agent.py`, agent loop/providers/memory | `agent/AGENTS.md` |
| `cli.py`, CLI/config/setup/update | `hermes_cli/AGENTS.md` |
| gateway/adapters/session lifecycle | `gateway/AGENTS.md` |
| tools, `toolsets.py`, `model_tools.py` | `tools/AGENTS.md` |
| plugins and plugin loading | `plugins/AGENTS.md` |
| TUI/JSON-RPC, `ui-tui/` | `tui_gateway/AGENTS.md` |
| dashboard and web routers | `web/AGENTS.md` |
| desktop | `apps/desktop/AGENTS.md`; additionally `apps/desktop/src/AGENTS.md` when changing that subtree |
| shipped/optional skills and curator | `skills/AGENTS.md` |
| cron/kanban dispatch | `cron/AGENTS.md` |
| a new platform adapter | `gateway/platforms/ADDING_A_PLATFORM.md` |

The nested guides remain the source for their domain-specific invariants. Long-form background is in `website/docs/developer-guide/`; do not preload the whole directory. Resolve a missing guide before changing the contract it governs.
