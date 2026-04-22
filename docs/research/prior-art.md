# Prior Art — Per-Chat Isolated Instances Refactor

**Date:** 2026-04-22
**Branch:** `multi-instance`
**Scope:** OSS survey for the per-chat-isolated-instance rebuild (see `docs/architecture/audit.md`). The decisions already fixed going in: **Python orchestrator**, **`cloudflared` sidecar per instance with one named tunnel each**, tunnel state in Postgres, `projctl` CLI (Go preferred, Python acceptable), host-mode retained as legacy for one release. This document evaluates every project called out in the research brief against those constraints and ends with a build-vs-buy matrix and a net recommendation.

**How to read a row:** each entry has a Summary, a License + self-host line, a Verdict (one of **adopt** / **fork** / **steal-pattern** / **skip**), the integration point (or a one-line skip reason), and Risks. URLs are cited for every maintenance or behaviour claim so that a reviewer can spot-check. Dates below are as of April 2026.

---

## A. Per-session / per-agent sandbox runtimes

### Daytona — `daytonaio/daytona`

**Summary.** Daytona is "a secure and elastic infrastructure runtime for AI-generated code execution and agent workflows," with per-sandbox isolated kernels, filesystems, and network stacks, a ~90 ms cold start claim, and first-class snapshot/restore for persisting sessions across restarts. It ships a Docker-Compose open-source deployment from the `docker/` directory alongside a managed SaaS (app.daytona.io). Source: [github.com/daytonaio/daytona](https://github.com/daytonaio/daytona). The control plane is active: `v0.168.0` on 2026-04-21, 183 releases, 2,380 commits on main.

**License + self-host.** AGPL-3.0. Yes — full self-host via the bundled Compose stack or a distributed deployment; Kubernetes is *not* required. AGPL is the live issue here — any modification we ship to users, including UI tweaks, obliges us to publish equivalent source. For an internal orchestrator that end-users never touch directly, this is manageable; if Daytona is ever exposed as a user-facing surface the AGPL trigger tightens.

**Verdict: skip (for now), re-evaluate in the next refactor.** Daytona would absorb a large chunk of what we'd otherwise build — sandbox provisioning, snapshots, resource limits — but the fit with our decisions is weaker than it first looks:

- **Networking model mismatch.** Daytona's sandboxes have their own network stack; our sidecar tunnel topology assumes we control the Docker network the instance lives on, so we can attach `cloudflared` and reach the app over the compose bridge. The Daytona docs do not describe a supported way to join a Daytona sandbox to an external Compose network, and doing it out-of-band would fight the control plane.
- **Named Cloudflare tunnels.** Zero first-class support; we'd be wrapping Daytona sandboxes with our own tunnel sidecar anyway, so the sandbox manager only replaces "container lifecycle" — which is already 30 lines of docker-py for us.
- **AGPL on the control plane** is a drag on any future open-sourcing of the orchestrator.
- **Runtime fit.** Daytona is oriented toward agent code-execution sessions (Python/TS/JS interpreters, snapshot-heavy). Our per-chat instance is a Laravel + MySQL + Redis + Vite *dev stack*, closer to a full dev environment than a code interpreter. Snapshots matter less when the source of truth is a git branch.

**Risks of adopting:** AGPL, second control plane to operate alongside ARQ, unclear upgrade path, networking impedance against our tunnel model.

---

### Cloudflare Sandbox SDK — `cloudflare/sandbox-sdk`

**Summary.** Per-session isolated containers running on Cloudflare Workers/Durable Objects infrastructure, exposed through an SDK with `exec`, `readFile`, `writeFile`, and a `/workspace` filesystem. Apache-2.0, actively released (`@cloudflare/sandbox@0.8.11` on 2026-04-15). Source: [github.com/cloudflare/sandbox-sdk](https://github.com/cloudflare/sandbox-sdk).

**License + self-host.** Apache-2.0 on the SDK. **Hosted-only for the runtime** — the sandbox itself runs on Cloudflare's edge; you cannot run the container plane yourself. This is the operating model of Workers-for-Platforms, not open infrastructure.

**Verdict: skip.** The stack we need to fit inside one instance is Laravel + PHP-FPM + MySQL + Redis + Node/Vite. Cloudflare Containers (the underlying runtime) is a single-container-per-sandbox model with limited egress and no sidecar semantics in the sense we need: you cannot run `cloudflared` as a sibling of your app, because the whole point of the platform is that *Cloudflare is the network*. In practical terms:

- No supported MySQL-as-a-sibling; you'd have to use Hyperdrive/D1/external.
- No compose-network semantics; our "one cloudflared per instance" decision doesn't translate.
- Persistent filesystem across sessions is possible via Durable Object storage but is the wrong substrate for `/var/lib/mysql` + `/workspaces/<instance>`.

One-line reason: the platform removes the exact degrees of freedom (network, sidecars, multi-process) that our decisions depend on. Revisit if we ever shrink an instance to "one container running the user's app with external DB" — not this quarter.

**Risks of adopting:** vendor lock-in, no local dev story, would force a full stack redesign.

---

### Hocus — `hocus-dev/hocus`

**Summary.** Self-hosted Firecracker-microVM-backed dev environments; positioned as a Gitpod/Codespaces alternative. MIT. Source: [github.com/hocus-dev/hocus](https://github.com/hocus-dev/hocus).

**License + self-host.** MIT. **Archived on 2024-09-28** — the README states "the project has been discontinued due to the underlying startup dissolving" and recommends DevPod or Coder instead.

**Verdict: skip (dead upstream).** Do not adopt archived infrastructure as a load-bearing dependency. The MIT source is still there to read for ideas on Firecracker lifecycle, but we're on Docker Compose and not moving to microVMs this refactor.

**Risks of adopting:** no maintainer, no security patches.

---

### DevPod — `loft-sh/devpod`

**Summary.** A client-only CLI that takes a `devcontainer.json` and stands up a matching environment against a pluggable backend (local Docker, SSH host, cloud VM, K8s). MPL-2.0, actively maintained, `v0.6.15` on 2025-03-10 with 210 releases. Source: [github.com/loft-sh/devpod](https://github.com/loft-sh/devpod).

**License + self-host.** MPL-2.0. **Client-only — there is no server backend to host.** The "server" is whatever compute you give it.

**Verdict: steal-pattern.** Don't adopt the binary — our orchestrator needs server-side state, job queueing, and chat-facing control, none of which DevPod provides. *Do* adopt the **`devcontainer.json` spec** as the second-tier input for `projctl` behind our `guide.md`. Rationale:

- `devcontainer.json` is already a standard (Codespaces, VS Code Dev Containers, DevPod). A large fraction of target repos already ship one.
- It gives us a no-ambiguity answer to "what image does this instance need?" for projects that don't have a `guide.md`.
- Parsing it is cheap (it's JSON with comments); we don't need DevPod's resolver — the spec is at [containers.dev](https://containers.dev).

**Integration point:** `projctl detect` → if `guide.md` exists, use it; else if `.devcontainer/devcontainer.json` exists, synthesize the compose template from it; else fall through to Railpack-style language detection.

**Risks:** spec drift (minor), having to support `features` composition (minor — we can start with the image-only subset).

---

### Agent Infra Sandbox (AIO Sandbox) — `agent-infra/sandbox`

**Summary.** "All-in-One Sandbox for AI Agents" — a single Docker image bundling Shell, Files, Browser (VNC + CDP), VSCode-in-browser, Jupyter, and pre-wired MCP servers. Apache-2.0, `v1.0.0.152` on 2025-11-12. Docker-Compose and Kubernetes deploy recipes included. Source: [github.com/agent-infra/sandbox](https://github.com/agent-infra/sandbox).

**License + self-host.** Apache-2.0. Fully self-hostable as a container image.

**Verdict: steal-pattern.** Not a fit as *the* per-chat instance (too opinionated, bundles too much, image is heavy for an every-chat spawn), but it's the strongest public reference for **what goes inside the instance when the user expects browser/IDE surfaces too**. Specifically we should mirror:

- The MCP-server-on-localhost convention for shell / files / browser access, so agent tools have one URL shape regardless of transport.
- The code-server-in-container pattern when we add the "open IDE" button to the chat (a later milestone — but the door is worth keeping open).
- The VNC-as-last-resort pattern for browser-assisted debugging of the running app.

**Integration point:** reference architecture for a future `instance-tools` sidecar, not a dependency we ship today.

**Risks:** image size if we ever did adopt it wholesale.

---

### Kubernetes-sigs Agent Sandbox — `kubernetes-sigs/agent-sandbox`

**Summary.** A K8s CRD (`Sandbox`) + controller for isolated, stateful, single-pod agent workloads; ships extensions for templates, claims, warm pools. Apache-2.0, `v0.3.10` on 2026-04-08. Source: [github.com/kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox).

**License + self-host.** Apache-2.0. Yes, but **Kubernetes-only** — not a Compose option.

**Verdict: skip (wrong runtime substrate).** Our deploy target today is Docker Compose on a single VPS. The CRD pattern (stable hostname, warm pool, persistent storage) is exactly the shape we want if we ever graduate to K8s — note the warm-pool primitive specifically, because pre-warming an instance to beat cold-start is on our future wish list — but porting there is out of scope for the current refactor. Flag this for the *next* architecture round.

**Risks of adopting now:** forces K8s before we need it.

---

### Alibaba OpenSandbox — `alibaba/OpenSandbox`

**Summary.** A general-purpose sandbox platform with pluggable container runtimes (gVisor / Kata / Firecracker) and multi-language SDKs (Python, Java/Kotlin, JS, C#, Go). Apache-2.0, 1,142 commits, active through April 2026, 10.2 k stars. Works on Docker locally and scales to K8s. Source: [github.com/alibaba/OpenSandbox](https://github.com/alibaba/OpenSandbox).

**License + self-host.** Apache-2.0. Self-hostable, Docker or K8s.

**Verdict: skip (over-scoped).** The pluggable-isolation story (swap Docker → gVisor or Firecracker without changing callers) is genuinely attractive as an escape hatch, but:

- Our current threat model — trusted operator running trusted-ish user repos — doesn't justify gVisor/Firecracker complexity today.
- OpenSandbox wants to be *the* execution platform; dropping it in means rewriting our worker task model around its SDK, which is far bigger than the audit's "adopt X to do Y" bar.
- Our `cloudflared`-sidecar decision needs first-class network control that OpenSandbox does not make easy — its isolation story is at odds with our "let the instance reach its own sibling tunnel" requirement.

Keep as an escape hatch reference if we later need per-instance runtime isolation beyond what Docker gives.

**Risks of adopting now:** runtime-swap introduces operational surface we can't currently debug.

---

### Restyler's `awesome-sandbox` list

**Summary.** Curated matrix of sandboxing solutions. Source: [github.com/restyler/awesome-sandbox](https://github.com/restyler/awesome-sandbox). Scanned for anything not already in our brief.

Additions worth noting:

- **`e2b`** — Firecracker-based agent sandbox runtime, primarily hosted; self-host path is available but thin. Same shape as Daytona, with a slightly more agent-native API. Skip for the same reasons as Daytona.
- **`microsandbox`** — libkrun-powered self-hosted microVM platform, `v0.1.0` on 2025-05-20. Too early for production.
- **WebContainers / CodeSandbox / Replit / Fly.io / Gitpod / Coder** — all either hosted platforms or full-weight cloud IDEs, not per-chat sandbox primitives.
- **gVisor / Kata / nsjail** — lower-level isolation primitives. Out of scope until threat model changes.

**Verdict: skip (reference only).**

---

## B. Cloudflare Tunnel ingress plane

### DockFlare — `ChrispyBacon-dev/DockFlare`

**Summary.** A self-hosted control plane that watches Docker labels, reconciles Cloudflare Tunnel *ingress rules*, *DNS CNAMEs*, and *Access policies* in one loop. Labels like `dockflare.hostname=app.example.com` + `dockflare.service=http://svc:port` trigger full state reconciliation. GPL-3.0, `v3.1.0` on 2026-04-16, 1,076 commits on stable. Source: [github.com/ChrispyBacon-dev/DockFlare](https://github.com/ChrispyBacon-dev/DockFlare).

**License + self-host.** GPL-3.0. Fully self-hosted — it *is* the control plane.

**Verdict: steal-pattern.** DockFlare is the closest public analogue to what we're building on the tunnel side, but two things make full adoption the wrong move:

1. **Topology mismatch with our decision.** DockFlare is built for the *shared-multiplexer* model — one `cloudflared` per host, with an `ingress:` table that grows and shrinks as containers come and go. We picked **sidecar per instance**: one tunnel per instance, one hostname per tunnel, no multiplexing. DockFlare's ingress-reconciliation code solves a problem we chose not to have.
2. **GPL-3.0 on the whole control plane** would virally bind our orchestrator if we embedded it. Running it as a separate process is allowed, but then we're operating two control planes for no net gain.

What to steal (read the code, reimplement ~150–300 lines of Python against the Cloudflare API):

- **DNS CNAME automation**: create a CNAME per hostname pointing to `<tunnel-id>.cfargotunnel.com`, delete on teardown, idempotent, retry with backoff.
- **Zone-aware record placement**: pick the right zone ID for a given hostname when we run multiple apex domains.
- **Drift reconciliation**: on startup, list tunnels + DNS + active instances, delete orphans, heal missing records.
- **Label conventions**: we'll use environment variables on the instance row in Postgres, not Docker labels, but the *set of fields* (hostname, target service, port, access rules) is the right shape to copy.

**Integration point:** new module `services/tunnel_provisioner.py`, called at instance-up and instance-teardown. Replaces the quick-tunnel logic in the existing `tunnel_service.py`; reuses the existing spawn/health-check plumbing.

**Risks:** Cloudflare API rate limits, token scope (need DNS:edit + Tunnel:edit + Access:edit on the target zone), drift if manual edits happen in the Cloudflare dashboard.

---

### `DownToWorld/laravel-devops`

**Summary.** A production Laravel Compose template where everything (Laravel/nginx, MySQL, Redis, Meilisearch, Minio, Soketi) sits on a dedicated `cloudflared` bridge network and only the `cloudflared` container talks to the outside. No host ports published. Most recent release `v0.7.6` on 2024-02-02, one maintainer, modest activity — "maintained but not buzzing." Source: [github.com/DownToWorld/laravel-devops](https://github.com/DownToWorld/laravel-devops).

**License + self-host.** License is not declared on the landing page — needs a direct check before we copy. Fully self-hostable; it's literally a compose file.

**Verdict: steal-pattern.** This is the exact **no-published-ports Laravel topology** we want for our per-instance compose template:

- Dedicated bridge network named `cloudflared` (or in our case, `instance-<id>`).
- `cloudflared` container is the only one with outbound egress; everything else is reachable only via the bridge.
- Services addressed by their Compose service names (`laravel.test:8000`, `mysql:3306`, `redis:6379`, `cloudflared:8080`).
- Vite dev service on the same bridge, reachable at `cloudflared:5173` from the tunnel.

**Integration point:** template for the generated `docker-compose.instance.yml` emitted by `projctl up` for Laravel projects. Keep our own volume layout, our own image pins, our own port allocator.

**Risks:** single-maintainer upstream; license unclear — treat as read-only inspiration, not a dependency.

---

### `jonas-merkle/container-cloudflare-tunnel`

**Summary.** Minimal `cloudflared` sidecar Compose setup driven by a tunnel-token env var and a config volume. LGPL-3.0, `v1.0.0` on 2024-12-17. Source: [github.com/jonas-merkle/container-cloudflare-tunnel](https://github.com/jonas-merkle/container-cloudflare-tunnel).

**License + self-host.** LGPL-3.0. Self-hostable (it's a compose file).

**Verdict: steal-pattern.** Good starting skeleton for the sidecar block of our instance compose template — specifically the env-var-driven `TUNNEL_TOKEN` pattern and the host-config-isolation approach. We won't inherit the repo; we'll crib ~20 lines.

**Integration point:** the `cloudflared` service block in `docker-compose.instance.yml`.

**Risks:** LGPL is fine for dependency-style linking but matters if we ever vendor code; we're not.

---

### Sam Rhea — "a sidecar named cloudflared"

**Summary.** The canonical mental-model post: run `cloudflared` as a sidecar, have it connect to the application via `localhost` (K8s pod) or the Docker network (Compose), and only outbound connections cross the firewall. Published 2019-07-20. Source: [blog.samrhea.com/posts/2019/sidecar-cloudflared](https://blog.samrhea.com/posts/2019/sidecar-cloudflared/). Not on `blog.cloudflare.com` — the brief assumed the wrong host.

**Verdict: skip (reference read, not a dependency).** The mental model is already baked into our decision.

---

## C. Markdown-runbook → executable-steps CLI

### Runme — `stateful/runme`

**Summary.** Turns fenced code blocks in a Markdown file into executable steps. Supports shell / bash / zsh / Python / Ruby / JS / TS. Ships a Go CLI, a VS Code extension, and has Dagger integration (there is a `.dagger` folder and `dagger.json` in the repo). Env vars persist across cells; cells can be listed (`runme list`), printed, and run by name. Apache-2.0. 303 releases, `v3.16.10` in March 2026. Source: [github.com/stateful/runme](https://github.com/stateful/runme).

**License + self-host.** Apache-2.0. It's a CLI binary — self-host is meaningless in the hosted-service sense; we just ship the binary alongside `projctl`.

**Verdict: adopt (as a library / subprocess under `projctl`).** This is the clearest "don't build from scratch" win in the whole survey. Runme already solves:

- Parsing fenced code blocks with named steps and language tags.
- Per-cell env inheritance.
- Interactive and non-interactive execution.
- A stable CLI surface (`runme run <step-name>`) we can shell out to.

What we own on top:

- **The `guide.md` schema** — which fenced blocks are canonical (`install`, `migrate`, `up`, `health`), what success-check each one must pass, what the minimum-viable guide looks like.
- **Structured JSON log emission** — Runme's output is human-shaped; we wrap its stdout/stderr and emit our own `step_started` / `step_failed` / `step_ok` events keyed to an instance ID for the chat UI.
- **Success-check layer** — Runme runs a block, we run the check after (an HTTP probe, a `docker compose ps` filter, a container-log grep).
- **LLM fallback dispatch** — on failure, we build the context envelope (see D) and hand off.
- **Go vs Python for `projctl`.** Because Runme is Go, a Go `projctl` makes embedding it as a library straightforward (via the `runme/v3` Go packages) rather than shelling to a binary. That's the current lead design; Python + subprocess is the fallback.

**Integration point:** `projctl up` = "parse `guide.md` → for each required step, invoke Runme → post-run probe → structured log → on failure, call LLM fallback." Total new code is probably 400–700 lines of Go (or Python).

**Risks:** Runme breaking-change in CLI surface (manageable — we pin a version); Runme's fencing conventions (we'll adopt theirs and document). No license risk — Apache-2.0 is plumbing-friendly.

---

### mdrb — `andrewbrey/mdrb`

**Summary.** A Deno/TS tool that turns Markdown into an executable runbook of TS/JS code blocks with optional shell via `dax`. MIT, `3.0.4` on 2024-10-19, 22 releases. Source: [github.com/andrewbrey/mdrb](https://github.com/andrewbrey/mdrb).

**License + self-host.** MIT, standalone CLI.

**Verdict: skip.** One-line reason: Deno/TS-only and our orchestrator is Python; adopting it means adding a second runtime for strictly less capability than Runme.

**Risks:** moot.

---

### Runbook.md — `kjkuan/Runbook.md`

**Summary.** Bash-only literate-programming runbooks. Nice `Step/` and `Task/` function conventions, `set -eEo pipefail` enforced, stack traces back to markdown line numbers. BSD-2-Clause, small repo (37 commits), Shell 100%. Source: [github.com/kjkuan/Runbook.md](https://github.com/kjkuan/Runbook.md).

**License + self-host.** BSD-2-Clause, CLI.

**Verdict: skip.** Bash-only loses us Python/Node steps which most target projects will need (composer, npm, artisan). The error-to-markdown-line mapping is a nice idea worth copying, but Runme already gives us that.

**Risks:** maintenance velocity unclear.

---

### `braintree/runbook`

**Verdict: skip** — Ruby DSL, not markdown. Not in scope.

---

### Dagger — `dagger/dagger`

**Summary.** A programmable-pipeline engine with SDKs in Go / Python / TS / PHP / Java / .NET / Elixir / Rust, a daemon-backed execution engine, typed artifacts, caching DAG, reusable modules. Apache-2.0, `v0.20.6` on 2026-04-16, 855 releases. Source: [github.com/dagger/dagger](https://github.com/dagger/dagger).

**License + self-host.** Apache-2.0. Self-hostable (the engine runs locally or in CI).

**Verdict: skip.** Dagger solves a different problem — "build this code graph reliably with caching" — not "execute these human-readable runbook steps." Our contract with users is `guide.md` in plain markdown that they read and edit; Dagger's contract is SDK code they import. Adopting Dagger would either push that SDK into the user's repo (unfriendly) or wrap it behind our own DSL (rebuilding Runme, badly). Reconsider only if we find ourselves wanting DAG-level caching across steps, which `guide.md` doesn't need.

**Risks of adopting:** daemon operational cost, SDK-in-user-repo ergonomics, opaque failure modes for end users reading markdown.

---

## D. LLM-as-fallback for failed steps

### `dimitris-norce/selfhealing-action`

**Summary.** An experimental GitHub Action that, on build failure, calls an LLM (via LangChain + OpenAI) to propose a fix. The README explicitly warns: "Experimental repository ... for research and educational purposes only." GPL-3.0. 3 commits total, 0 stars. Source: [github.com/dimitris-norce/selfhealing-action](https://github.com/dimitris-norce/selfhealing-action).

**License + self-host.** GPL-3.0. N/A — it's a GHA.

**Verdict: skip (the code), steal-pattern (the idea).** Nothing shippable here — the repo flags its own lack of retry limits and single-file scope. But the *pattern* of "on failure, call LLM with structured context, apply a bounded patch" is the exact shape of our fallback, so it's worth citing as the conceptual ancestor.

**What to lift into our design:** the context envelope we ship to the LLM on a failed step. Our proposed envelope (this is the recommendation the brief asked for):

1. **Instance identity** — instance id, chat id, project slug, branch, hostname, `cloudflared` tunnel state.
2. **Failing step definition** — the literal `guide.md` block that failed, its declared success check, its language.
3. **Failure signal** — exit code, the last N lines of stdout+stderr (N=200 default, configurable).
4. **Relevant guide context** — the preceding prose paragraph in `guide.md` (author's intent) and the immediately prior successful step, so the model knows the state the environment is in.
5. **Repo fingerprint** — detected tech stack (Composer/NPM/Python), lockfile hashes, `.env.example` keys (values redacted).
6. **Structured output contract** — model MUST return exactly one of: `{"action":"patch","files":[...]}`, `{"action":"shell","cmd":"..."}`, `{"action":"skip","reason":"..."}`, or `{"action":"give_up","reason":"..."}`. No free-form prose. Enforce via JSON schema at the model boundary.
7. **Redaction pass** — before the envelope leaves the host, run it through the existing `host_guard` + a regex/entropy redactor: `AWS_`, `CLOUDFLARE_`, `DATABASE_URL`, `AUTHORIZATION`, anything matching high-entropy base64/hex ≥ 32 chars.
8. **Turn budget** — max N fallback turns per step, configured at the `guide.md` level, enforced by the orchestrator.

**Integration point:** `services/fallback_envelope.py` builds the envelope; `worker/tasks/step_repair.py` invokes it after a Runme step failure.

**Risks of stealing the pattern:** token cost if context is too large (we bound it), prompt injection from user repo content (we restrict the model's action surface).

---

### LiteLLM — `BerriAI/litellm`

**Summary.** A Python-first gateway with 100+ provider support, a Router that offers retries / model fallbacks / cost caps / spend tracking / OpenAI-compatible errors. Deployable as a proxy or embedded as a library (`from litellm import completion`). 44.3 k stars, 1,308 releases, latest on 2026-04-22. MIT. Source: [github.com/BerriAI/litellm](https://github.com/BerriAI/litellm).

**License + self-host.** MIT (with a commercial tier for enterprise proxy features, but the Router and SDK are fully open). Self-host either as an embedded library or as the proxy server.

**Verdict: adopt (library mode).** Not as a replacement for `claude_agent_sdk` — that stays, because it's where our tool-use loop lives. LiteLLM sits **underneath** for the non-agent LLM fallback call in (D): "one-shot, structured output, with fallback + cost cap + retries."

It is orthogonal to the Anthropic SDK (the Anthropic SDK is single-provider and does not do router-level fallback, retry policy, or cost caps). The two coexist cleanly:

- `claude_agent_sdk` continues to drive the master-agent bootstrap path (which is being deprecated but still lives for legacy projects) and the interactive chat loop.
- LiteLLM Router drives `projctl`'s LLM fallback — one shot in, structured JSON out, retry up to N times, fall back Claude → GPT → Gemini if Claude is down, hard cost cap per instance per day.

**Integration point:** `providers/llm/router.py` wraps LiteLLM Router with our provider config from `PlatformConfig`. Used exclusively by `step_repair.py`. Chat agents keep using the Anthropic SDK directly.

**Risks:** another dependency to track; LiteLLM's rapid-release cadence means we pin and update monthly. No lock-in — the Router interface is small and replaceable.

---

## E. Laravel + Vite + Vue HMR behind a tunnel

### Context and gotcha

Three issue threads were cited in the brief. Reading them produced the usual picture:

- **`vitejs/vite#9152`** — [github.com/vitejs/vite/issues/9152](https://github.com/vitejs/vite/issues/9152): reported 2022-07-16 for Laravel Sail + Vite + Vue 3, still open. The `server.hmr.host=localhost` path does not work under tunneled HTTPS; `clientPort` needs to be 443.
- **`laravel/mix#3154`** — 404 from the linked issue on the canonical laravel/mix repo. Treat the brief's reference as superseded history; Mix is retired in favour of Vite anyway.
- **`ddev/ddev#5018`** — [github.com/ddev/ddev/issues/5018](https://github.com/ddev/ddev/issues/5018): WSL2 + DDEV + Vite 4.3.9, HMR refuses at `127.0.0.1:5173`. Same root cause: the browser is asking for HMR on a port that is not the tunnel's public port.
- **`vitejs/vite#13564`** — [github.com/vitejs/vite/discussions/13564](https://github.com/vitejs/vite/discussions/13564): consolidated discussion for "HMR behind one tunnel." Consensus is `hmr.host=<tunnel>` + `hmr.clientPort=443` + `hmr.protocol=wss`, plus `server.allowedHosts` for Vite ≥ 5.

Community writeups confirm: [adampatterson.ca/development/setting-up-hot-module-reloading-with-cloudflared-and-vite](https://adampatterson.ca/development/setting-up-hot-module-reloading-with-cloudflared-and-vite/) and [blog.amirasyraf.com/vite-dev-cloudflare-tunnel](https://blog.amirasyraf.com/vite-dev-cloudflare-tunnel/) — the minimum working solution on a Cloudflare tunnel is `server.hmr.clientPort = 443`, with `protocol: 'wss'` and a matching `hmr.host` when using named tunnels.

### Minimum working Vite config for our per-instance template

```js
// vite.config.js — generated per instance
import { defineConfig } from 'vite'
export default defineConfig({
  server: {
    host: '0.0.0.0',
    origin: `https://${process.env.INSTANCE_HOST}`,
    allowedHosts: [process.env.INSTANCE_HOST],
    hmr: {
      host: process.env.INSTANCE_HMR_HOST,
      clientPort: 443,
      protocol: 'wss',
    },
  },
})
```

`INSTANCE_HOST` and `INSTANCE_HMR_HOST` are injected by the orchestrator at instance start.

### Tunnel-side note

Because we decided **hostname-per-instance** with a dedicated HMR subdomain (`hmr-<slug>.dev.<our-domain>` CNAME'd to the same per-instance tunnel, see `docs/architecture/per-chat-instances.md` §5.3), we avoid the path-based-routing WebSocket-upgrade trap that bites people who try to multiplex Vite under `/__vite` on the same hostname. Keep this decision documented; it removes a category of bug we would otherwise spend a week on. Path-based ingress remains out of scope.

---

## F. Lighter-weight reads

### Laravel Sail

**Summary.** Laravel's canonical Compose dev environment: `laravel.test` container, MySQL, Redis, Mailpit, optional Meilisearch / Selenium / MongoDB / Valkey / Typesense. Reference: [laravel.com/docs/12.x/sail](https://laravel.com/docs/12.x/sail).

**License + self-host.** MIT. Self-host — it's literally a compose file.

**Verdict: steal-pattern.** Use the Sail service layout as the starting shape of our Laravel per-instance template (service names, env-var plumbing, volume layout, healthcheck patterns). Drop the host port bindings entirely — replaced by the `cloudflared` sidecar.

**Risks:** none worth noting.

---

### Railway Nixpacks → Railpack

**Summary.** Nixpacks is in maintenance-only mode; Railway has published Railpack as its successor. Railpack is MIT, active (`v0.23.0` in March 2026), does zero-config language detection and produces an OCI image. Sources: [github.com/railwayapp/nixpacks](https://github.com/railwayapp/nixpacks) (deprecated), [github.com/railwayapp/railpack](https://github.com/railwayapp/railpack) (current).

**License + self-host.** MIT. Self-hostable (a binary).

**Verdict: steal-pattern.** When `projctl` has neither a `guide.md` nor a `devcontainer.json` to work from, we need *something* to detect the stack and emit a plausible instance template. Railpack's detection rules are the right reference — we don't need to adopt the binary (which wants to produce an OCI image, not a compose file), but we can mirror its detection heuristics (file presence → stack label) in a small Python module.

**Integration point:** `services/stack_detector.py`, used only in the fall-through path of `projctl detect`.

**Risks:** our heuristic diverges from Railpack's over time — acceptable, we don't depend on them.

---

### Gitpod / Coder / Codespaces idle-timeout UX

**What to copy.** Across the three, the UX pattern converges:

- **Default:** 30 minutes of inactivity.
- **Signal:** keystrokes, terminal input, or an active IDE connection. Losing the IDE connection typically shrinks the timeout (Gitpod drops to 5 min without IDE presence — [gitpod.io workspace lifecycle docs](https://www.gitpod.io/docs/configure/workspaces/workspace-lifecycle)).
- **Warning:** Codespaces surfaces "workspace will soon terminate" in the UI a short window before the timeout — exact timing is not publicly documented ([github.com/orgs/community/discussions/70543](https://github.com/orgs/community/discussions/70543)).
- **Config range:** Codespaces allows 5 min to 240 min; org admins can cap it ([docs.github.com / setting-your-timeout-period-for-github-codespaces](https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces)).
- **Resume:** the stopped state preserves the worktree and any persistent volumes; restart is "open workspace."

**Our target UX:**

- Default 30 min idle timeout per instance; configurable per chat (5–240 min).
- Activity signal = any message in the chat, any `projctl` command, any HTTP hit on the tunnel.
- At T-5 min, post a "instance will stop in 5 minutes, reply or click Keep Alive to continue" message into the chat.
- On timeout, `docker compose down` the instance (preserve volumes), tear down the tunnel, mark `Instance.status=stopped`. Worktree and DB volumes persist.
- Resume = "start instance" button in chat; reattaches to same hostname.

**Verdict: steal-pattern.** Reference these three products, don't depend on them.

---

## Build vs. Buy Matrix

| Component                   | Decision                    | Notes / what we own on top                                                                |
|-----------------------------|-----------------------------|-------------------------------------------------------------------------------------------|
| Orchestrator                | **build**                   | No OSS fits: we need per-chat instance lifecycle driven by Telegram/Slack/web-chat events, tied to ARQ + `WebChatSession`. Daytona and Hocus are the closest matches; one is AGPL + networking-mismatched, the other is archived. |
| Sandbox runtime             | **build** (Docker Compose)  | Per-instance `docker-compose.instance.yml` template generated by `projctl`. K8s-native options (agent-sandbox) are future work. |
| Tunnel control plane        | **steal-pattern:`DockFlare`** | We lift DNS-reconciliation + drift-heal patterns, not the daemon. Topology differs (sidecar-per-instance vs shared-multiplexer). |
| Tunnel data plane           | **adopt:`cloudflared`**     | Named tunnels, per-instance sidecar. Our existing `tunnel_service.py` keeps the spawn/monitor plumbing; switch `--url` for `tunnel run <named>`. |
| Runbook executor            | **adopt:`Runme`**           | Embed as Go library (or subprocess) under `projctl`. We own the `guide.md` schema, success checks, log envelope, LLM fallback dispatch. |
| LLM fallback router         | **adopt:`LiteLLM`**         | Library mode for the non-agent repair path. Retries, model fallback, cost cap. Anthropic SDK stays for the chat agent. |
| LLM fallback prompt/envelope| **build**                   | Context envelope shape is ours; modelled after `selfhealing-action`'s concept but nothing of theirs ships. |
| Reaper (idle timeout)       | **build**                   | No OSS does exactly what we need (chat-activity-linked). Gitpod/Codespaces/Coder UX pattern stolen. |
| Compose template (Laravel)  | **steal-pattern:`laravel-devops` + `Sail` + `container-cloudflare-tunnel`** | No-ports bridge network + Sail service layout + minimal cloudflared sidecar block. |
| IDE-in-browser surface      | **build (later)**, **steal-pattern:`agent-infra/sandbox`** | Not in this refactor; reference AIO Sandbox when we add the "open IDE" button. |
| DNS automation              | **build** (inspired by `DockFlare`) | ~200 lines of Python against the Cloudflare API. CNAME mint/delete, zone-aware placement, drift reconciliation. |
| Stack detector (fallback)   | **build** (inspired by `Railpack`) | Simple file-presence heuristics for projects with neither `guide.md` nor `devcontainer.json`. |
| `devcontainer.json` reader  | **steal-pattern:`DevPod` / spec** | Parse the spec directly; don't depend on DevPod. |
| `projctl` CLI               | **build** (Go preferred)    | Wraps Runme, talks to orchestrator over gRPC/HTTP, writes structured logs. Go lets us embed Runme as a library. |

**Justifications for every `build` cell** (one line each):

- **Orchestrator** — glue between chat events, ARQ, DB, tunnel, instance lifecycle; too bespoke for any OSS.
- **Sandbox runtime** — Docker Compose on a single VPS is the decision; nothing generic fits without fighting our tunnel topology.
- **LLM fallback prompt/envelope** — content is domain-specific (our `guide.md` schema + instance identity); no OSS prompt ships this.
- **Reaper** — "idle" means "no chat message in N minutes AND no tunnel hit AND no `projctl` call" — a TAGH-specific signal.
- **DNS automation** — DockFlare is GPL and topology-wrong; the subset we need is small enough to own.
- **Stack detector** — trivial heuristics; not worth a dependency.
- **`projctl` CLI** — the contract with users (`projctl up/down/logs/explain`) is ours.

---

## Net recommendation

Three projects are actually entering the codebase: **Runme** as the runbook executor embedded under `projctl`, **LiteLLM** as the LLM router for the step-repair fallback path, and **cloudflared** in named-tunnel mode as the tunnel data plane (already present, reconfigured). Four more we steal patterns from without taking a dependency: **DockFlare** for DNS + drift reconciliation against the Cloudflare API, **`DownToWorld/laravel-devops` + Laravel Sail + `jonas-merkle/container-cloudflare-tunnel`** for the no-published-ports Compose template, **Railpack** for fallback stack detection when a project has neither `guide.md` nor a `devcontainer.json`, and **Gitpod/Codespaces** for the idle-timeout warning UX. The rest — Daytona, Cloudflare Sandbox SDK, Hocus, DevPod (binary), Dagger, OpenSandbox, Kubernetes-sigs agent-sandbox — are skips for this refactor, each for a concrete reason (license, archived upstream, wrong runtime substrate, or fights our tunnel topology). What stays genuinely "build from scratch" is the per-chat orchestrator itself, the `guide.md` schema and success-check layer, the LLM fallback envelope, the activity-driven reaper, and the `projctl` CLI surface — precisely the parts where our contract with users lives, which is where owning the code pays off.
