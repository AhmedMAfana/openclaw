# VERIFICATION — Per-chat isolated instances

Record of the manual [quickstart.md](quickstart.md) walk-through on a
staging host, per Constitution Principle VII ("done and verified" —
merge only after observing the feature work end-to-end). Each section
below mirrors a quickstart step and MUST carry a `done/verified` or
`deferred` marker before this spec's branch merges to `main`.

Date of most recent walk-through: **pending** — to be filled by the
engineer who runs the full pass.

Staging host: **pending** — record hostname + commit SHA.

| § | Quickstart step | Status | Notes |
|---|-----------------|--------|-------|
| 1 | Golden-path provision → HMR → chat | **pending** | Verifies SC-002 cold-path + SC-005 HMR round-trip. |
| 2 | Adversarial cross-chat attempts fail | **pending** | Mirrors T032's assertion set against a running fleet. |
| 3 | Idle teardown → grace banner → resume | **pending** | 24h wait; fast-forward via `REAPER_DRY_RUN=0` + a short `idle_ttl_hours` override in `platform_config`. |
| 4 | Manual `/terminate` → destroyed → fresh | **pending** | T071's shape; run it as a UI click through the web chat. |
| 5 | HMR edit 100× over the tunnel | **pending** | Mirrors T054's assertion — p95 < 3 s. |
| 6 | Failing guide.md step → 3 LLM attempts → Retry resumes | **pending** | Mirrors T075; uses a test guide.md with a deliberate `cmd: 'false'` step. |
| 7 | Chat delete → full cascade | **pending** | Mirrors T085 — audit rows keyed by slug must be gone. |
| 8 | Teardown leaves zero residue | **pending** | Matches FR-006 assertions: no containers, volumes, secrets, CF tunnel, DNS records, workspace dir. |

## Running the walk-through

```bash
# On the staging host, checked out to the merge candidate:
export OPENCLOW_DB_TESTS=1
export OPENCLOW_E2E=1
export TAGH_DEV_STAGING_CF_ZONE=<your-dev-zone>

# Follow quickstart.md sections 1–8 in order. For each, paste the
# observed output or a screenshot link into the "Notes" column above
# and flip the status to `done/verified` or `deferred`.
docker compose up -d api worker
alembic upgrade head
```

Any `deferred` status must carry a line-item justification and a
follow-up task ID — either an existing T0xx or a new one in
`tasks.md`. Principle VII forbids "works on my machine" merges.
