---
name: User Feedback - Quality Standards
description: Critical quality standards — test before shipping, agentic loops, self-healing, production-ready fixes
type: feedback
---

All fixes must be root-level, production-ready. No runtime hacks, no band-aids.

**Why:** User is building a production platform. Anything that breaks on a fresh `docker compose up` is unacceptable. Runtime pip installs, placeholder workarounds, and "fix it later" approaches waste time and create hidden bugs.

**How to apply:**
- Every code change must work on a fresh container build (Dockerfile, not `docker exec pip install`)
- Test with Playwright before declaring something works
- If a dependency is needed, add it to pyproject.toml AND verify the Dockerfile picks it up
- If an API scope is needed, document it and check for it gracefully
- Don't leave dead code alongside new code — clean up fully
- Self-healing: if something can fail, handle it with clear error messages
