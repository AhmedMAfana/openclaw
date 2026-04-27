// File: rotate_git_token.go — `projctl rotate-git-token` cron loop.
//
// Every 45 minutes:
//   1. POST {"at": "<ISO8601>"} to
//      /internal/instances/<slug>/rotate-git-token with HMAC-SHA256.
//   2. Receive {token, expires_at, repo}.
//   3. Overwrite $HOME/.git-credentials with a single line:
//      https://x-access-token:<token>@github.com
//   4. chmod 600 the file.
//   5. export GITHUB_TOKEN=<token> for subsequent shells (via
//      $HOME/.profile.d/ drop-in).
//
// Exit codes match heartbeat.go — 0 clean, 1 fatal, 2 usage.
// On 503 (GitHub App outage) we silently wait for the next cron tick
// per FR-027c — the chat banner comes from a separate codepath.
package steps

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

// RotateOptions — `projctl rotate-git-token` config.
type RotateOptions struct {
	Slug       string        // INSTANCE_SLUG
	URL        string        // full POST endpoint
	Secret     string        // HMAC key
	Interval   time.Duration // default 45 min; tests override
	HTTPClient *http.Client
	Version    string
	// HomeDir is the shell home used for .git-credentials + .profile.d.
	// Default $HOME; tests pass a tmp dir.
	HomeDir string
}

// RotateResponse matches the contract response shape.
type RotateResponse struct {
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
	Repo      string `json:"repo"`
}

// RotateGitTokenLoop runs until ctx is cancelled. Returns nil on clean
// exit, non-nil only for fatal auth/404 conditions.
func RotateGitTokenLoop(ctx context.Context, opts RotateOptions) error {
	if opts.Slug == "" || opts.URL == "" || opts.Secret == "" {
		return fmt.Errorf("rotate-git-token: slug/url/secret all required")
	}
	if opts.Interval == 0 {
		opts.Interval = 45 * time.Minute
	}
	if opts.HTTPClient == nil {
		opts.HTTPClient = &http.Client{Timeout: 15 * time.Second}
	}
	if opts.HomeDir == "" {
		opts.HomeDir = os.Getenv("HOME")
		if opts.HomeDir == "" {
			opts.HomeDir = "/root"
		}
	}

	// Rotate once immediately on startup so the credential file exists
	// before the first `git push` — avoids a chicken-and-egg wait for
	// the first tick.
	if err := rotateOnce(ctx, opts); err != nil {
		if isFatalRotateError(err) {
			return err
		}
		fmt.Fprintf(os.Stderr, "rotate-git-token: initial rotation failed: %v (will retry)\n", err)
	}

	t := time.NewTicker(opts.Interval)
	defer t.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-t.C:
			if err := rotateOnce(ctx, opts); err != nil {
				if isFatalRotateError(err) {
					return err
				}
				fmt.Fprintf(os.Stderr, "rotate-git-token: %v (will retry)\n", err)
			}
		}
	}
}

func rotateOnce(ctx context.Context, opts RotateOptions) error {
	body := map[string]any{
		"at": time.Now().UTC().Format(time.RFC3339Nano),
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
		return &rotateTransientError{err: err}
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusOK:
		// Fall through to parse.
	case http.StatusUnauthorized, http.StatusNotFound, http.StatusConflict:
		return &rotateFatalError{status: resp.StatusCode}
	case http.StatusServiceUnavailable:
		// GitHub App outage per contract. Silently wait for next tick;
		// the chat banner comes from the orchestrator side.
		delay := 300
		if ra := resp.Header.Get("Retry-After"); ra != "" {
			if n, err := strconv.Atoi(ra); err == nil && n > 0 {
				delay = n
			}
		}
		return &rotateTransientError{err: fmt.Errorf(
			"503 GitHub App degraded; retry in %ds", delay)}
	case http.StatusTooManyRequests:
		return &rotateTransientError{err: fmt.Errorf("429 rate limited")}
	default:
		return &rotateTransientError{err: fmt.Errorf(
			"unexpected status %d", resp.StatusCode)}
	}

	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, 16*1024))
	if err != nil {
		return fmt.Errorf("read body: %w", err)
	}
	var parsed RotateResponse
	if err := json.Unmarshal(bodyBytes, &parsed); err != nil {
		return fmt.Errorf("parse body: %w", err)
	}
	if parsed.Token == "" {
		return fmt.Errorf("empty token in response")
	}

	// Write ~/.git-credentials atomically. Write to a sibling tmp file
	// + rename so a kill mid-write never corrupts the existing creds.
	credPath := filepath.Join(opts.HomeDir, ".git-credentials")
	line := fmt.Sprintf("https://x-access-token:%s@github.com\n", parsed.Token)
	if err := atomicWrite(credPath, []byte(line), 0o600); err != nil {
		return fmt.Errorf("write credentials: %w", err)
	}

	// Also emit GITHUB_TOKEN via ~/.profile.d/ so future shells see it.
	// `ash` (alpine) and `bash` both source $HOME/.profile on login,
	// which in most base images globs .profile.d/*.sh — if not, the
	// drop-in is harmless.
	profileDir := filepath.Join(opts.HomeDir, ".profile.d")
	if err := os.MkdirAll(profileDir, 0o700); err == nil {
		tokenShellLine := fmt.Sprintf("export GITHUB_TOKEN=%q\n", parsed.Token)
		_ = atomicWrite(
			filepath.Join(profileDir, "github_token.sh"),
			[]byte(tokenShellLine),
			0o600,
		)
	}

	return nil
}

// atomicWrite writes data to path via a tmp + rename. Preserves the
// mode on the final file, even if the caller's umask would have
// widened the tmp file's mode.
func atomicWrite(path string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".projctl-tmp-")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer func() {
		// Best-effort cleanup of the tmp on error paths.
		_ = os.Remove(tmpPath)
	}()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, path)
}

// --- Error taxonomy --------------------------------------------------

type rotateTransientError struct{ err error }

func (e *rotateTransientError) Error() string {
	return "rotate transient: " + e.err.Error()
}

type rotateFatalError struct{ status int }

func (e *rotateFatalError) Error() string {
	return fmt.Sprintf("rotate fatal: HTTP %d", e.status)
}

func isFatalRotateError(err error) bool {
	if err == nil {
		return false
	}
	_, ok := err.(*rotateFatalError)
	return ok
}
