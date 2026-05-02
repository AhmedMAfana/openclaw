# Generic `/e2e-pipeline` Skill Template

A project-agnostic prompt for bootstrapping a live end-to-end testing skill in
any Claude Code project. Inspired by TAGH Dev's `/e2e-pipeline` skill, refined
with patterns from the broader Claude Code / Playwright Agents ecosystem.

---

## Background

Where a static audit (typecheck, lint, contract checks) catches "module A
calls B but B doesn't know about it", a live e2e skill catches "the user
clicked Buy and no order was created". You want **both** — they cover
disjoint failure modes.

This template generates the *live* half. Pair it with a static-audit skill
(`/pipeline-audit` equivalent) for full coverage.

---

## Transferable principles (stack-agnostic)

1. **Live, not simulated** — drives the real system end-to-end, no mocks in the pipeline.
2. **Phase-structured** — pipeline split into named phases with explicit boundaries.
3. **Forensic capture per phase** — logs, config, communication traces, execution metadata dumped to `artifacts/<run-id>/<phase>/`.
4. **Honest reporting** — distinguishes "exercised live" vs "merely rendered".
5. **Two-tier fix policy on failure** — *hot-fix* to keep the run going, then *root-fix* in source; both documented.
6. **Failure classification** — env / infra / config / code (so the fix lands in the right layer).
7. **Gated by a static audit first** — runs only after the cheap static check passes.
8. **Multi-role agent split** — planner / reviewer / generator / executor / healer / reporter, not one mega-agent.
9. **Risk-ranked journeys** — test the top 3-5 user journeys by (business value × failure likelihood), not exhaustive coverage.
10. **Self-healing with bounded budget** — one retry per step, max N healed steps per run; past that it's a real bug.
11. **AI-assisted, not AI-autonomous** — humans pick journeys and accept the verdict.

---

## The prompt — paste verbatim into another project's Claude Code

```
Create a slash-command skill at .claude/skills/e2e-pipeline/ that drives a
LIVE end-to-end test of this project's primary user journeys. Project-
agnostic structure, project-specific phases. Follow this multi-agent design:

ROLES (orchestrated as sub-tasks; keep responsibilities cleanly separated):
  - PLANNER  : explores the app + reads the code, picks the top 3-5 user
               journeys ranked by (business value × failure likelihood),
               writes them as natural-language specs.
  - REVIEWER : critiques the planner's spec for missing assertions,
               hallucinated selectors, untestable steps. Blocks until clean.
  - GENERATOR: turns each NL spec into executable steps (Playwright if UI,
               curl/HTTP client if API). Auto-extracts a Page Object Model
               on first run; reuses it after.
  - EXECUTOR : runs the steps against the LIVE system, captures forensics.
  - HEALER   : on failure, reads error + DOM + screenshot + recent diffs,
               proposes a fix (drifted selector, race, missing wait,
               changed copy), applies it, retries the step ONCE. If still
               failing, classifies (env/infra/config/code) and stops.
  - REPORTER : writes REPORT.md distinguishing "exercised live" vs "merely
               rendered", with verdict PASS / PASS-with-fixes / FAIL.

PHASE-1 — DISCOVER (do first, ask only if ambiguous):
  - Detect stack (language, framework, how the app starts, where logs
    live, where state lives — DB, queue, cache, files).
  - Detect a UI driver: prefer Playwright MCP for UI; fall back to HTTP
    client for backend-only services.
  - Identify the static-audit equivalent (typecheck, lint, contract tests).
    Run it as preflight; abort the e2e run if it fails.
  - Detect external integrations that need sandbox creds (payments, email,
    SMS, OAuth). List them; mark which will be faked and call that out
    explicitly in the report.

PHASE-2 — DESIGN the phase list. Typical shape, adapt to the product:
  0. preflight       — services up, creds present, static audit clean
  1. setup           — pick/create the test subject (user, tenant, project)
  2. entry           — first real user action through the real entry point
  3. core-action     — the value-delivering action of the product
  4. side-effects    — verify downstream artifacts (DB rows, queue jobs,
                       webhooks, emails, files, external API calls). This
                       is the phase that proves the system actually worked,
                       not just that the UI rendered the right banner.
  5. second-actor    — isolation / multi-tenant / concurrency check
  6. teardown        — cleanup path; verify nothing leaks
  7. report          — write REPORT.md

PHASE-3 — WRITE SKILL.md with:
  - Trigger conditions (before release, after touching <core files>,
    before claiming a feature done).
  - Prerequisites (services running, creds present).
  - Per-phase script: (a) the real action, (b) OBSERVABLE success
    criteria (not "UI showed X"), (c) the forensic dump command.
  - Failure handling: HEALER retries once with a proposed fix. If still
    failing, classify (env / infra / config / code), apply hot-fix to
    keep the run going, then root-fix in source. Document both in the
    report. Re-run only the failed phase.
  - Reporting rules: REPORT.md must distinguish "actually exercised"
    from "merely rendered", list every artifact path, and state the
    verdict honestly. Sandbox/faked integrations are called out by name.

PHASE-4 — WRITE helpers under scripts/e2e/:
  - preflight.<ext>  — JSON readiness probe.
  - capture.<ext>    — phase artifact dump. Capture FOUR planes per phase
    (forensic taxonomy from agent-platform research):
       1. logs            (app, worker, db, proxy)
       2. config snapshot (env vars present (names only, not values),
                           feature flags, service versions)
       3. communication traces (HTTP requests/responses, queue jobs,
                                webhooks fired, emails sent)
       4. execution metadata   (timings per phase, DB row counts,
                                redis keys, container/process state)
    Output to artifacts/e2e-<timestamp>/<phase>/ — gitignored.

PHASE-5 — Generated Page Object Model lives at
  .claude/skills/e2e-pipeline/pom/  (committed; survives across runs).
  On first run, GENERATOR extracts it. On later runs, GENERATOR reuses
  and extends it. HEALER updates entries when selectors drift.

PHASE-6 — One-line invocation note in CLAUDE.md or README.

CONSTRAINTS:
  - No mocks in the live pipeline. If something MUST be faked, name it
    in the report.
  - Every phase needs an OBSERVABLE success criterion, not a UI banner.
  - Repeatable: must run twice in a row without manual cleanup between.
  - HEALER retries are bounded (1 retry per step, max 3 healed steps per
    run) — past that, it's a real bug, not flakiness.
  - Humans pick the top journeys and accept the verdict. The skill is
    AI-assisted, not AI-autonomous. Say so in SKILL.md.

DELIVERABLES:
  - .claude/skills/e2e-pipeline/SKILL.md
  - .claude/skills/e2e-pipeline/pom/  (empty on first install)
  - scripts/e2e/preflight.<ext>
  - scripts/e2e/capture.<ext>
  - .gitignore entry for artifacts/
  - One-line invocation note in CLAUDE.md or README

WORKFLOW:
  1. Run DISCOVER, show me the detected stack + proposed top-5 journeys
     + proposed phase list. Wait for confirmation.
  2. After confirmation, write all files in one pass.
  3. Do a dry-run of preflight.<ext> and report what it returned.
```

---

## Companion: black-box e2e vs white-box logic check

This skill is a **black-box outside-in tester**. It follows the *request flow*
(user action → side effects), not the *function-call logic between internals*.
For internal contract drift ("backend emits event X, does the frontend handle
X?", "every `enqueue_job('foo')` matches a registered worker function"),
build a sister `/pipeline-audit` skill that reads source code and checks
implicit contracts statically. The two skills together give you the same
coverage TAGH Dev has, on any stack.

| | Black-box e2e (this skill) | White-box logic check (sister skill) |
|---|---|---|
| Drives | Real user entry points | Source code analysis |
| Verifies | Observable outcomes | Contracts between modules |
| Catches | "User clicked Buy, no order created" | "A passes wrong field to B" |
| When to run | Before release, after touching pipeline files | Pre-commit, on every change |

---

## Sources / prior art

- [lackeyjb/playwright-skill](https://github.com/lackeyjb/playwright-skill) — Claude Code skill for Playwright automation
- [agentmantis/test-skills](https://github.com/agentmantis/test-skills) — Page Object Model + lifecycle agent skills
- [neonwatty/qa-skills](https://github.com/neonwatty/qa-skills) — Multi-user workflow QA pipeline for Claude Code
- [firstloophq/claude-code-test-runner](https://github.com/firstloophq/claude-code-test-runner) — Natural-language E2E test runner
- [Building an AI QA Engineer with Claude Code and Playwright MCP — alexop.dev](https://alexop.dev/posts/building_ai_qa_engineer_claude_code_playwright/)
- [Write automated tests with Claude Code using Playwright Agents — Shipyard](https://shipyard.build/blog/playwright-agents-claude-code/)
- [Playwright Skill Claude Code: 82 E2E Tests — TestDino](https://testdino.com/blog/playwright-skill-claude-code/)
- [Self-healing E2E tests with LLMs — itnext](https://itnext.io/self-healing-e2e-tests-reducing-manual-maintenance-efforts-using-llms-db35104a7627)
- [Self-Healing Code: Autonomous E2E Testing for Coding Agents](https://beyondthehype.dev/p/self-healing-agents-through-e2e-testing)
- [How to implement self-healing tests with AI — refluent](https://medium.com/refluent/how-to-implement-self-healing-tests-with-ai-640b0c8139a4)
- [Beyond the hype: Multi-agent system for E2E test generation — Metropolis](https://www.metropolis.io/blog/beyond-the-hype-building-a-multi-agent-system-for-e2e-test-generation)
- [Agentic Testing: 80% E2E Coverage — Autonoma](https://www.getautonoma.com/blog/agentic-testing-full-coverage)
- [Octomind](https://octomind.dev/) — AI-powered E2E platform
- [Forensic Analysis of Artifacts from AutoGen — ACM ARES](https://dl.acm.org/doi/10.1145/3664476.3670908) — four-plane artifact taxonomy
- [AI Agent Testing Automation — Sitepoint 2026](https://www.sitepoint.com/ai-agent-testing-automation-developer-workflows-for-2026/)
