# Feature Specification: Enterprise Platform Hardening

**Feature Branch**: `feat/enterprise-upgrade`
**Created**: 2026-05-10
**Status**: Draft
**Input**: User description: "docs/enterprise-upgrade-plan.md"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Operator Has Full System Visibility (Priority: P1)

An operator managing the AI Dev Orchestrator platform can open a single dashboard and immediately understand the health of the entire system: which background jobs are running, how many are failing, what the LLM cost is per day, and whether any chat sessions are degraded — without digging through raw logs or connecting to the server.

**Why this priority**: Blind operation is the biggest risk. Without visibility, failures are discovered by end users, not the team. This unblocks everything else — you can't fix what you can't see.

**Independent Test**: Can be fully tested by starting the system, triggering a background job, and verifying the metrics dashboard updates within 30 seconds. Delivers standalone value: operators can monitor production before any other hardening is in place.

**Acceptance Scenarios**:

1. **Given** the system is running, **When** an operator opens the metrics dashboard, **Then** they see real-time counts of active jobs, failed jobs, API request rates, and error rates — all without accessing the server directly.
2. **Given** an LLM agent call completes, **When** the operator views the tracing interface, **Then** they see the full trace: input tokens, output tokens, cost, duration, and whether the prompt cache was hit.
3. **Given** an unhandled exception occurs in any service, **When** the exception fires, **Then** it is automatically captured in the error tracking service with the user ID, chat session ID, and job name attached — within 5 seconds of the error.
4. **Given** a background job fails permanently, **When** the operator views the admin panel, **Then** the failed job appears in the dead-letter queue with its error message and retry button visible.

---

### User Story 2 - Users Get Fast, Consistent Responses Under Load (Priority: P2)

An end user sending messages in the AI chat interface receives responses without noticeable slowdowns, even when many other users are active at the same time. The system handles traffic spikes by distributing load rather than degrading for everyone.

**Why this priority**: Performance is the most user-visible quality attribute. The gains from this story are immediate and require no user-facing UI changes.

**Independent Test**: Can be tested by simulating 50 concurrent users sending chat messages and measuring response latency before and after — delivers value as a measurable p99 latency improvement.

**Acceptance Scenarios**:

1. **Given** 50 users are simultaneously active, **When** each sends a message, **Then** the system processes all requests without any individual user waiting more than 2x the single-user baseline.
2. **Given** the database has been idle for an extended period, **When** a user sends a message, **Then** the response arrives without a connection-related delay or error.
3. **Given** a background job has been running for over 10 minutes, **When** it exceeds its time budget, **Then** it is automatically cancelled and the user receives a failure notification — the system does not hang indefinitely.

---

### User Story 3 - Background Jobs Never Silently Disappear (Priority: P3)

When a background job (like provisioning a container, executing a task, or rotating a token) fails permanently, it is visible to operators and can be retried manually — it is never silently dropped. Duplicate job submissions (from retries or network issues) are automatically deduplicated and never cause double provisioning.

**Why this priority**: Silent job loss causes user-visible corrupted state (e.g., instance stuck in "provisioning" forever). This is a reliability foundation that makes the system trustworthy.

**Independent Test**: Can be tested by submitting the same provisioning job twice rapidly and verifying only one instance is created, plus by triggering a deliberate job failure and confirming it appears in the dead-letter queue.

**Acceptance Scenarios**:

1. **Given** a job fails after exhausting retries, **When** an operator views the dead-letter queue, **Then** the job appears with its error, timestamp, and a one-click retry option.
2. **Given** two identical provisioning requests arrive within 60 seconds (e.g., from a double-click or network retry), **When** both are processed, **Then** only one instance is created — the second is silently deduplicated.
3. **Given** the worker service is restarted mid-job, **When** the worker comes back online, **Then** in-flight jobs either complete gracefully or land in the dead-letter queue — they are never silently lost.
4. **Given** an external service (e.g., Cloudflare DNS API) is experiencing an outage, **When** a job calls it, **Then** the job fails fast with a clear error rather than hanging for minutes waiting for a timeout.

---

### User Story 4 - API Cannot Be Abused or Overwhelmed (Priority: P4)

The API surface is hardened so that no single user or IP address can overwhelm the system, all responses carry standard security headers, and the login endpoint is protected against brute-force attacks. Per-user containers are isolated so that one user's container cannot affect another's.

**Why this priority**: Security hardening becomes urgent as soon as the platform opens to more users. The rate limiting and headers changes are low-effort and high-value.

**Independent Test**: Can be tested by sending 61 rapid requests to any endpoint and verifying the 61st is rejected, then inspecting any response header for security attributes.

**Acceptance Scenarios**:

1. **Given** an unauthenticated IP sends more than 60 requests per minute, **When** the 61st request arrives, **Then** the server responds with a rate-limit rejection — no requests beyond the limit are processed.
2. **Given** a user attempts more than 5 login attempts per minute, **When** the 6th attempt is made, **Then** it is rejected with a rate-limit response.
3. **Given** any API response is returned, **When** the response headers are inspected, **Then** they include standard security attributes preventing clickjacking, content sniffing, and cross-origin resource leakage.
4. **Given** a per-user container is running, **When** a process inside attempts to gain additional system privileges, **Then** the attempt is blocked at the container level.

---

### User Story 5 - System Can Run on Multiple Servers Without Data Conflicts (Priority: P5)

Multiple copies of the API service and the worker service can run simultaneously on different machines. They share the same database, cache, and job queue without producing split-brain conditions, duplicate jobs, or connection exhaustion. Adding a new server node requires only configuration, not code changes.

**Why this priority**: This is the foundation for horizontal scaling. It's placed last because it builds on all the other hardening phases — idempotency, connection pooling, and Redis HA all need to be in place first.

**Independent Test**: Can be tested by running two API instances and two worker instances simultaneously, submitting 100 jobs, and verifying all 100 complete exactly once with correct results.

**Acceptance Scenarios**:

1. **Given** two worker instances are running, **When** 50 jobs are submitted, **Then** the jobs are distributed across both workers — no job is processed twice.
2. **Given** the primary database connection proxy fails over, **When** it recovers, **Then** running services reconnect automatically without needing a restart.
3. **Given** a user is streaming a chat response via SSE, **When** the API node serving them goes down mid-stream, **Then** the client automatically reconnects to another node and resumes the stream from the last received event.

---

### Edge Cases

- What happens when the metrics dashboard itself becomes unavailable — does it affect the main application?
- How does the dead-letter queue behave if it grows very large (thousands of entries)?
- What happens to an active chat SSE stream when the serving API node restarts during a deployment?
- How does rate limiting handle a legitimate burst from a single trusted IP (e.g., an automated test suite)?
- What happens if both the primary and replica Redis nodes go down simultaneously?
- How does container security hardening interact with applications that legitimately need elevated capabilities (e.g., a user's app that uses Docker inside Docker)?

---

## Requirements *(mandatory)*

### Functional Requirements

**Observability**

- **FR-001**: The system MUST expose real-time operational metrics (job counts, error rates, latency distributions, LLM token usage) accessible without direct server access.
- **FR-002**: Every LLM agent invocation MUST be traceable end-to-end, showing input, output, token cost, cache hit status, and duration.
- **FR-003**: All unhandled exceptions MUST be automatically captured and associated with their originating user session, chat ID, and job type.
- **FR-004**: Operators MUST be able to view and manually retry permanently failed jobs from the admin interface.

**Performance**

- **FR-005**: The system MUST support at least 9 concurrent API request handlers without a code change.
- **FR-006**: Database connections MUST be validated before use to prevent stale-connection errors after idle periods.
- **FR-007**: Each background job type MUST have a maximum execution time after which it is automatically cancelled.

**Reliability**

- **FR-008**: Submitting the same job twice within 60 minutes with the same idempotency key MUST result in exactly one execution.
- **FR-009**: When the worker restarts, in-flight jobs MUST either complete or be written to a dead-letter queue — never silently dropped.
- **FR-010**: When an external dependency (Cloudflare API, GitHub API) is unavailable, dependent jobs MUST fail fast rather than blocking indefinitely.
- **FR-011**: Live streaming connections MUST tolerate a 100-second idle period without being terminated by intermediate proxies.
- **FR-012**: Clients MUST be able to reconnect to an interrupted stream and resume from the last delivered event.

**Security**

- **FR-013**: All API endpoints MUST enforce per-IP and per-user request rate limits.
- **FR-014**: The login endpoint MUST enforce a stricter rate limit than general API endpoints.
- **FR-015**: All API responses MUST include standard security headers preventing clickjacking, MIME sniffing, and cross-origin data leakage.
- **FR-016**: Cross-origin access MUST be restricted to an explicitly configured allowlist of origins.
- **FR-017**: Processes running inside per-user containers MUST be prevented from acquiring additional system privileges.

**Multi-Server Readiness**

- **FR-018**: Multiple worker instances MUST be able to process jobs from the same queue without executing any job more than once.
- **FR-019**: The system MUST support a connection proxy in front of the database so that total database connections scale independently of the number of application instances.
- **FR-020**: The cache and job queue infrastructure MUST support automatic failover so that a single node failure does not cause a complete system outage.

### Key Entities *(include if feature involves data)*

- **Dead-Letter Queue Entry**: A record of a permanently failed background job, including its type, error message, original arguments, failure timestamp, and retry status.
- **Metrics Time Series**: A sequence of operational measurements (counts, latencies, rates) stored for dashboard display and alerting.
- **LLM Trace**: A complete record of one LLM agent call, capturing input, output, token usage, cost, duration, and relationship to its parent chat session.
- **Rate Limit Counter**: A per-IP or per-user counter with a rolling time window, used to enforce request limits.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: API response time at the 99th percentile drops by at least 30% compared to the pre-hardening baseline under equivalent load.
- **SC-002**: Operators can identify and locate the root cause of any system error within 5 minutes using the observability tooling alone — no server access required.
- **SC-003**: Zero background jobs are silently lost during a planned worker restart — all in-flight jobs either complete or appear in the dead-letter queue within 60 seconds of shutdown.
- **SC-004**: Sending the same provisioning request twice in rapid succession results in exactly one instance created — verified across 100 test runs with zero double-provisioning events.
- **SC-005**: A single IP address is blocked after exceeding the rate limit, while other users are unaffected — verified with concurrent load tests.
- **SC-006**: All API responses carry the required security headers, verified by automated header inspection across every endpoint.
- **SC-007**: Two API service instances and two worker instances run simultaneously and process 100 jobs with zero duplicate executions and zero job loss.
- **SC-008**: Daily LLM token cost is visible in the observability dashboard and segmented by project, enabling operators to identify the top 3 cost drivers within 10 minutes.

---

## Assumptions

- The system currently runs on a single server and will be migrated to multi-server over a period of weeks, not days — the hardening phases can be shipped incrementally.
- Observability infrastructure (metrics, tracing, error tracking) will be self-hosted on the same server initially, then migrated to a managed service if volume warrants it.
- The LLM tracing tool will be deployed in cloud mode first (no infrastructure overhead) before a self-hosted migration is considered.
- Container security hardening applies to per-user application containers; the platform's own infrastructure containers (database, cache, worker) are treated as trusted internal services.
- The shared workspace filesystem problem (Phase 5.1) is deferred to the last phase and may be solved by keeping workspaces inside per-instance containers synced via git, rather than shared network storage.
- Rate limits documented in the plan (60/min for public endpoints, 5/min for login) are starting defaults and will be tuned based on observed production traffic patterns.
- A connection proxy for the database is required for multi-server mode but is optional for single-server deployments — it will be introduced at the same time as the first second server is added.
- Redis high-availability (Sentinel) is required before adding a second server node; the system is not safe for multi-server operation without it.
