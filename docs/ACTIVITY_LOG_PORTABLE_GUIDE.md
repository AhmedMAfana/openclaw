# Portable Activity Log + Dozzle — Copy to Any Project

## The Concept (30 seconds)

```
Your App (any language, any framework)
    │
    │  Every important event → append one JSON line to a file
    │
    ▼
activity.jsonl          ← append-only, one JSON object per line
    │
    ├── tail -f           ← terminal monitoring
    ├── jq filters        ← CLI querying
    ├── Dozzle            ← web UI for Docker logs
    └── your code         ← query programmatically
```

**Three pieces:**
1. **A log writer** — appends JSON lines to a file (10 lines of code)
2. **A log reader** — queries the file with filters (20 lines of code)
3. **Dozzle** — 5-line Docker service for web UI

That's it. No Prometheus, no Grafana, no ELK stack, no database.

---

## Piece 1: The Log Writer (any language)

### Concept

One function. Takes an event type and a dict. Writes one JSON line. Thread-safe. Never crashes the caller.

### Python

```python
import json, time, os, threading

LOG_FILE = "/app/logs/activity.jsonl"
_lock = threading.Lock()

def log_event(event_type: str, data: dict):
    entry = {"ts": time.time(), "type": event_type, **data}
    with _lock:
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass  # Never crash the caller
```

### Node.js

```javascript
const fs = require('fs');
const path = require('path');

const LOG_FILE = '/app/logs/activity.jsonl';

function logEvent(type, data = {}) {
  const entry = { ts: Date.now() / 1000, type, ...data };
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, JSON.stringify(entry) + '\n');
  } catch (e) {} // Never crash the caller
}
```

### Go

```go
package actlog

import (
    "encoding/json"
    "os"
    "sync"
    "time"
)

var (
    logFile = "/app/logs/activity.jsonl"
    mu      sync.Mutex
)

func LogEvent(eventType string, data map[string]interface{}) {
    data["ts"] = float64(time.Now().UnixMilli()) / 1000
    data["type"] = eventType
    mu.Lock()
    defer mu.Unlock()
    f, err := os.OpenFile(logFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
    if err != nil { return }
    defer f.Close()
    json.NewEncoder(f).Encode(data)
}
```

### Bash (for shell scripts)

```bash
log_event() {
    local type="$1" ; shift
    echo "{\"ts\":$(date +%s.%N),\"type\":\"$type\",$@}" >> /app/logs/activity.jsonl
}

# Usage:
log_event "deploy" "\"version\":\"1.2.3\",\"status\":\"started\""
```

---

## Piece 2: Typed Helpers (optional but recommended)

Wrap `log_event` with typed functions for your domain. These are examples — change the event types to match YOUR application.

### For a Web API

```python
def log_request(method, path, status, duration_ms, user_id=""):
    log_event("request", {
        "method": method, "path": path, "status": status,
        "duration_ms": duration_ms, "user_id": user_id,
    })

def log_error(source, error, context=""):
    log_event("error", {"source": source, "error": str(error)[:500], "context": context})

def log_job(job_name, status, duration_s=0, result=""):
    log_event("job", {"job": job_name, "status": status, "duration_s": duration_s, "result": result})

def log_auth(action, user_id, success, ip=""):
    log_event("auth", {"action": action, "user_id": user_id, "success": success, "ip": ip})
```

### For an ML Pipeline

```python
def log_train(model, epoch, loss, accuracy, duration_s):
    log_event("train", {"model": model, "epoch": epoch, "loss": loss, "accuracy": accuracy, "duration_s": duration_s})

def log_inference(model, input_size, output_size, latency_ms):
    log_event("inference", {"model": model, "input_size": input_size, "output_size": output_size, "latency_ms": latency_ms})

def log_data(pipeline, rows_in, rows_out, errors):
    log_event("data", {"pipeline": pipeline, "rows_in": rows_in, "rows_out": rows_out, "errors": errors})
```

### For a Multi-Agent AI System

```python
def log_agent(agent_name, action, tool="", duration_ms=0, success=True):
    log_event("agent", {"agent": agent_name, "action": action, "tool": tool, "duration_ms": duration_ms, "success": success})

def log_llm_call(model, tokens_in, tokens_out, duration_ms, cost=0):
    log_event("llm_call", {"model": model, "tokens_in": tokens_in, "tokens_out": tokens_out, "duration_ms": duration_ms, "cost": cost})

def log_tool_call(tool, params, duration_ms, success, output_size):
    log_event("tool_call", {"tool": tool, "params": str(params)[:200], "duration_ms": duration_ms, "success": success, "output_size": output_size})
```

---

## Piece 3: The Log Reader

### Python (copy-paste ready)

```python
import json

def query_log(log_file, event_type="", last_n=50, since_ts=0, filters=None):
    """Query JSONL log file. Returns list of matching entries."""
    filters = filters or {}
    matches = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and entry.get("type") != event_type:
                    continue
                if since_ts and entry.get("ts", 0) < since_ts:
                    continue
                if filters and not all(entry.get(k) == v for k, v in filters.items()):
                    continue
                matches.append(entry)
    except FileNotFoundError:
        return []
    return matches[-last_n:]


def get_stats(log_file):
    """Get summary statistics."""
    entries = query_log(log_file, last_n=999999)
    by_type = {}
    for e in entries:
        t = e.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
    return {"total": len(entries), "by_type": by_type}
```

### CLI with jq (no code needed)

```bash
# All errors
cat activity.jsonl | jq 'select(.type == "error")'

# Last 10 events
tail -10 activity.jsonl | jq .

# Events in last 5 minutes
SINCE=$(date -d '5 minutes ago' +%s)
cat activity.jsonl | jq "select(.ts > $SINCE)"

# Average request duration
cat activity.jsonl | jq 'select(.type == "request") | .duration_ms' | awk '{s+=$1; n++} END {print s/n "ms avg"}'

# Error count by source
cat activity.jsonl | jq -r 'select(.type == "error") | .source' | sort | uniq -c | sort -rn

# Success rate for tools
cat activity.jsonl | jq 'select(.type == "tool_call")' | jq -s '[.[] | .success] | {total: length, success: [.[] | select(. == true)] | length}'
```

---

## Piece 4: Dozzle (Docker log viewer)

### Add to any docker-compose.yml

```yaml
services:
  # ... your existing services ...

  dozzle:
    image: amir20/dozzle:latest
    container_name: dozzle
    ports:
      - "9999:8080"   # Change port as needed
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    restart: unless-stopped
```

**That's it.** Open `http://localhost:9999` — you see all container logs in real-time with search and filtering.

### What Dozzle gives you (zero config)

- Real-time log streaming from all containers
- Search and filter across containers
- Regex search support
- Container health status
- Log download
- Multi-container view
- Dark mode

### What Dozzle does NOT give you

- Metrics/dashboards (use the stats function for that)
- Alerting (add later if needed)
- Log persistence beyond container lifecycle (your JSONL file handles this)

---

## Piece 5: Integration Pattern

### Where to hook in

```
┌─────────────────────────────────────────────────────┐
│                   Your Application                   │
│                                                      │
│  HTTP Handler ──→ log_request(method, path, ...)     │
│  Error Catch  ──→ log_error(source, error, ...)      │
│  Background Job ─→ log_job(name, status, ...)        │
│  Auth Event   ──→ log_auth(action, user, ...)        │
│  External API ──→ log_tool_call(api, duration, ...)  │
│                                                      │
│  All go to: activity.jsonl (one file, append-only)   │
└─────────────────────────────────────────────────────┘
```

### Rules

1. **Never let logging crash the caller** — wrap in try/except, always
2. **Append-only** — never read-modify-write the log file
3. **One JSON object per line** — JSONL format, not a JSON array
4. **Thread-safe** — use a lock (mutex) on the write
5. **Truncate large fields** — cap strings at 200-500 chars
6. **Include timestamp** — always `ts` as Unix float
7. **Include type** — always `type` as string for filtering
8. **Keep it flat** — avoid deep nesting, jq works best on flat objects

### Log Rotation (when file gets big)

Simple approach — rotate daily or at 100MB:

```bash
# Cron job or startup script:
MAX_SIZE=100000000  # 100MB
if [ -f activity.jsonl ] && [ $(stat -f%z activity.jsonl 2>/dev/null || stat -c%s activity.jsonl) -gt $MAX_SIZE ]; then
    mv activity.jsonl "activity.$(date +%Y%m%d-%H%M%S).jsonl"
fi
```

Or in Python:

```python
import os

def _rotate_if_needed():
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 100_000_000:
        rotated = LOG_FILE.replace(".jsonl", f".{int(time.time())}.jsonl")
        os.rename(LOG_FILE, rotated)
```

---

## Complete Copy-Paste Template

### For a new Python project

Create `observability.py`:

```python
"""Drop-in activity log. Copy this file to any project."""

import json, time, os, threading

LOG_FILE = os.environ.get("ACTIVITY_LOG", "activity.jsonl")
_lock = threading.Lock()

def log_event(event_type: str, data: dict):
    entry = {"ts": time.time(), "type": event_type, **data}
    with _lock:
        try:
            os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

def query(event_type="", last_n=50):
    matches = []
    try:
        with open(LOG_FILE) as f:
            for line in f:
                if not line.strip(): continue
                try: entry = json.loads(line)
                except: continue
                if event_type and entry.get("type") != event_type: continue
                matches.append(entry)
    except FileNotFoundError:
        return []
    return matches[-last_n:]

def stats():
    entries = query(last_n=999999)
    by_type = {}
    for e in entries:
        t = e.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    return {"total": len(entries), "by_type": by_type}
```

Use it:

```python
from observability import log_event, query, stats

# Log anything
log_event("startup", {"version": "1.0", "env": "prod"})
log_event("request", {"path": "/api/users", "status": 200, "ms": 42})
log_event("error", {"msg": "connection timeout", "service": "db"})

# Query
recent_errors = query("error", last_n=10)
dashboard = stats()
```

### Docker Compose addition

```yaml
  dozzle:
    image: amir20/dozzle:latest
    ports:
      - "9999:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    restart: unless-stopped
```

---

## When to Upgrade Beyond This

| Signal | Upgrade To |
|---|---|
| Multiple machines / containers | Loki + Grafana (centralized logs) |
| Need dashboards with graphs | Prometheus + Grafana |
| Need alerting (PagerDuty, Slack) | Alertmanager or Grafana alerts |
| Need distributed tracing | OpenTelemetry + Jaeger |
| Activity log > 1GB/day | Structured logging to stdout + Loki |
| Team of 5+ operators | Full ELK or Datadog |

Until you hit those signals, **JSONL + jq + Dozzle** covers 95% of observability needs.
