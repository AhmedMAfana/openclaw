#!/usr/bin/env bash
# Capture forensic artifacts for one phase of /e2e-pipeline.
#
# Usage:
#   scripts/e2e/capture.sh <run-dir> <phase-name> [instance-slug]
#
# Layout produced:
#   <run-dir>/<phase>/
#     services.txt       — `docker compose ps`
#     ps.txt             — `docker ps -a` (per-instance containers too)
#     api.log            — last 200 lines of api logs
#     worker.log         — last 200 lines of worker logs
#     instance.log       — per-instance compose stack logs (if slug given)
#     instance.json      — instance row from DB (if slug given)
#     redis-keys.txt     — taghdev:* keys (cap state, locks)
#     timestamp.txt      — UTC timestamp of capture
#
# Idempotent: running twice for the same phase overwrites, doesn't merge.
# Read-only: this script never mutates any of the systems it inspects.

set -euo pipefail

RUN_DIR="${1:?usage: capture.sh <run-dir> <phase> [slug]}"
PHASE="${2:?usage: capture.sh <run-dir> <phase> [slug]}"
SLUG="${3:-}"

OUT="$RUN_DIR/$PHASE"
mkdir -p "$OUT"

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$OUT/timestamp.txt"

# --- Compose state -------------------------------------------------------
docker compose ps --format "table {{.Service}}\t{{.State}}\t{{.Status}}" \
  > "$OUT/services.txt" 2>&1 || true

docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" \
  > "$OUT/ps.txt" 2>&1 || true

# --- Core service logs --------------------------------------------------
docker compose logs --tail=200 api    > "$OUT/api.log"    2>&1 || true
docker compose logs --tail=200 worker > "$OUT/worker.log" 2>&1 || true

# --- Per-instance stack (only if slug provided) -------------------------
if [[ -n "$SLUG" ]]; then
  COMPOSE_PROJECT="inst-$SLUG"
  docker compose -p "$COMPOSE_PROJECT" logs --tail=200 \
    > "$OUT/instance.log" 2>&1 || echo "no per-instance compose for $COMPOSE_PROJECT" \
    > "$OUT/instance.log"

  # DB row for the instance
  docker compose exec -T postgres \
    psql -U postgres -d taghdev -At -c \
    "SELECT row_to_json(i) FROM instances i WHERE slug='$SLUG' LIMIT 1;" \
    > "$OUT/instance.json" 2>&1 || true
fi

# --- Redis state (cap counters, upstream-degradation flags) -------------
docker compose exec -T redis redis-cli --raw KEYS 'taghdev:*' \
  > "$OUT/redis-keys.txt" 2>&1 || true

# --- Recent stream events (best-effort tail) ----------------------------
grep -E "controller\.add_data|stream_event|instance_(failed|provisioning|ready|terminated|degraded)" \
  "$OUT/api.log" 2>/dev/null | tail -50 > "$OUT/stream-events.tail.log" || true

echo "captured: $OUT"
