// File: heartbeat.go — `projctl heartbeat` daemon loop.
//
// Runs every 60 seconds while at least one of:
//   - the Vite dev server is running
//   - a task is executing
//   - an interactive shell is attached
//
// POSTs to /internal/instances/<slug>/heartbeat with an HMAC-SHA256
// signature over the raw request body using $HEARTBEAT_SECRET.
// Contract: specs/001-per-chat-instances/contracts/heartbeat-api.md
//
// Spawned by tini inside the `app` container — NOT a separate
// container (arch doc §7). Exit codes:
//   0  clean shutdown on signal
//   1  fatal (unrecoverable config / HMAC / 401 / 404)
//   2  usage error
package steps

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// HeartbeatOptions — `projctl heartbeat` config, all sourced from env.
type HeartbeatOptions struct {
	Slug       string        // from INSTANCE_SLUG
	URL        string        // full POST endpoint (HEARTBEAT_URL)
	Secret     string        // HMAC key (HEARTBEAT_SECRET)
	Interval   time.Duration // default 60s; overridable via env for tests
	HTTPClient *http.Client  // injectable for tests; nil → default
	Version    string        // projctl version for X-Projctl-Version
	// ProbeSignals is called each tick to decide WHETHER to heartbeat.
	// Default in production is `defaultSignals` (checks dev server,
	// task marker, shell). Tests inject their own.
	ProbeSignals func() HeartbeatSignals
}

// HeartbeatSignals mirrors the orchestrator's HeartbeatSignals dataclass.
// Any true field causes a heartbeat POST; all-false ticks skip the POST
// so an idle instance goes quiet (and drifts into grace correctly).
type HeartbeatSignals struct {
	DevServerRunning bool `json:"dev_server_running"`
	TaskExecuting    bool `json:"task_executing"`
	ShellAttached    bool `json:"shell_attached"`
}

// HeartbeatLoop runs until ctx is cancelled. Returns nil on clean exit,
// non-nil for fatal auth/404 conditions the caller should NOT retry.
func HeartbeatLoop(ctx context.Context, opts HeartbeatOptions) error {
	if opts.Slug == "" || opts.URL == "" || opts.Secret == "" {
		return fmt.Errorf("heartbeat: slug/url/secret all required")
	}
	if opts.Interval == 0 {
		opts.Interval = 60 * time.Second
	}
	if opts.HTTPClient == nil {
		opts.HTTPClient = &http.Client{Timeout: 10 * time.Second}
	}
	if opts.ProbeSignals == nil {
		opts.ProbeSignals = defaultSignals
	}

	t := time.NewTicker(opts.Interval)
	defer t.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			signals := opts.ProbeSignals()
			if !signals.DevServerRunning && !signals.TaskExecuting && !signals.ShellAttached {
				// Idle tick — send NOTHING so the reaper observes quiet.
				continue
			}
			if err := postOnce(ctx, opts, signals); err != nil {
				if isFatalHeartbeatError(err) {
					return err
				}
				// Transient — keep looping. Next tick retries.
				fmt.Fprintf(os.Stderr, "heartbeat: %v (will retry)\n", err)
			}
		}
	}
}

// postOnce builds and sends one heartbeat. Separate function so tests
// can drive it synchronously without running a full ticker.
func postOnce(ctx context.Context, opts HeartbeatOptions, signals HeartbeatSignals) error {
	body := map[string]any{
		"at":      time.Now().UTC().Format(time.RFC3339Nano),
		"signals": signals,
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", opts.URL, bytes.NewReader(raw))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Signature", signBody(opts.Secret, raw))
	if opts.Version != "" {
		req.Header.Set("X-Projctl-Version", opts.Version)
	}

	resp, err := opts.HTTPClient.Do(req)
	if err != nil {
		return &heartbeatTransientError{err: err}
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)

	switch resp.StatusCode {
	case http.StatusOK:
		return nil
	case http.StatusUnauthorized, http.StatusNotFound:
		// Contract: the instance was likely re-provisioned with a new
		// secret, or destroyed. NEVER retry these — exit and let
		// compose supervise the restart.
		return &heartbeatFatalError{status: resp.StatusCode}
	case http.StatusConflict:
		// Status=terminating/destroyed/failed. Stop heartbeating.
		return &heartbeatFatalError{status: resp.StatusCode}
	case http.StatusTooManyRequests:
		// 429 — honor Retry-After if present, else default 1s.
		delay := 1
		if ra := resp.Header.Get("Retry-After"); ra != "" {
			if n, err := strconv.Atoi(ra); err == nil && n > 0 && n < 60 {
				delay = n
			}
		}
		time.Sleep(time.Duration(delay) * time.Second)
		return &heartbeatTransientError{err: fmt.Errorf("429 rate limited")}
	default:
		return &heartbeatTransientError{err: fmt.Errorf("unexpected status %d", resp.StatusCode)}
	}
}

// signBody returns the "hmac-sha256=<hex>" header value per the
// heartbeat-api.md contract. Wrapper in one place so rotate_git_token.go
// + explain.go reuse identically.
func signBody(secret string, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return "hmac-sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// defaultSignals probes the three signal sources the contract cares about.
// A failing probe is read as "absent" — never heartbeat on a guess.
func defaultSignals() HeartbeatSignals {
	return HeartbeatSignals{
		DevServerRunning: probeHTTP("http://localhost:5173"),
		TaskExecuting:    fileExists("/var/lib/projctl/task_running"),
		ShellAttached:    anyShellAttached(),
	}
}

// probeHTTP returns true iff a quick GET yields any non-5xx response.
// Used for the Vite dev-server signal.
func probeHTTP(url string) bool {
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode < 500
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// anyShellAttached returns true if `ps` reports at least one bash/sh
// process with a TTY inside the container. Cheap; runs < 50 ms.
func anyShellAttached() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "ps", "-eo", "tty,comm", "--no-headers").Output()
	if err != nil {
		return false
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		tty := fields[0]
		if tty == "?" || tty == "" {
			continue
		}
		comm := fields[1]
		if comm == "bash" || comm == "sh" || comm == "zsh" || comm == "fish" {
			return true
		}
	}
	return false
}

// --- Error taxonomy --------------------------------------------------

type heartbeatTransientError struct{ err error }

func (e *heartbeatTransientError) Error() string {
	return "heartbeat transient: " + e.err.Error()
}

type heartbeatFatalError struct{ status int }

func (e *heartbeatFatalError) Error() string {
	return fmt.Sprintf("heartbeat fatal: HTTP %d", e.status)
}

func isFatalHeartbeatError(err error) bool {
	if err == nil {
		return false
	}
	_, ok := err.(*heartbeatFatalError)
	return ok
}
