// Package state persists projctl's step-level progress to /var/lib/projctl/state.json.
//
// Spec: specs/001-per-chat-instances/research.md §7 + GUIDE_SPEC.md §5.
//
// Lives on a named per-instance Docker volume (tagh-inst-<slug>-projctl-state)
// so it survives container restart but is destroyed by `compose down -v`.
// Keyed by step name (not index) so guide.md edits that add/remove/reorder
// steps are tolerated.
//
// If the stored `guide_version` (SHA-256 of guide.md) no longer matches the
// current guide, ALL step outcomes are considered invalid: the guide changed,
// start over.
package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

// StepStatus values mirror the contracts/projctl-stdout.schema.json event types.
type StepStatus string

const (
	StatusSuccess StepStatus = "success"
	StatusFailed  StepStatus = "failed"
)

// Step is one recorded outcome.
type Step struct {
	Status     StepStatus `json:"status"`
	Attempt    int        `json:"attempt,omitempty"`
	FinishedAt string     `json:"finished_at,omitempty"`
	LastError  string     `json:"last_error,omitempty"`
	Skipped    bool       `json:"skipped,omitempty"`
}

// State is the on-disk JSON root object.
type State struct {
	GuideVersion    string          `json:"guide_version"`
	Steps           map[string]Step `json:"steps"`
	LastHeartbeatAt string          `json:"last_heartbeat_at,omitempty"`
}

// Default on-disk path. Kept as a variable (not const) so tests can override.
var DefaultPath = "/var/lib/projctl/state.json"

// Load reads state from path. If the file is missing, returns a fresh State
// bound to the given guideVersion. If the guide hash has changed, all step
// entries are dropped AND a new fresh State is returned (tracked.Invalidated
// is set so the caller can emit a JSON-line event about the reset).
type LoadResult struct {
	State       *State
	Invalidated bool // true iff the stored guide version didn't match
}

func Load(path, guideVersion string) (LoadResult, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return LoadResult{State: fresh(guideVersion)}, nil
	}
	if err != nil {
		return LoadResult{}, fmt.Errorf("read state: %w", err)
	}
	var s State
	if err := json.Unmarshal(b, &s); err != nil {
		// Corrupt state file is better treated as empty than fatal —
		// the worst case is redoing every step, which is what we'd do
		// on a guide-version mismatch anyway.
		return LoadResult{State: fresh(guideVersion), Invalidated: true}, nil
	}
	if s.GuideVersion != guideVersion {
		return LoadResult{State: fresh(guideVersion), Invalidated: true}, nil
	}
	if s.Steps == nil {
		s.Steps = make(map[string]Step)
	}
	return LoadResult{State: &s}, nil
}

// Save writes the state atomically: write to `path.tmp`, fsync, rename.
// Atomic rename means a mid-write crash never leaves a truncated file.
func Save(path string, s *State) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("mkdir state dir: %w", err)
	}
	tmp := path + ".tmp"
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return fmt.Errorf("open tmp: %w", err)
	}
	if _, err := f.Write(b); err != nil {
		f.Close()
		return fmt.Errorf("write: %w", err)
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return fmt.Errorf("fsync: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}

// RecordSuccess marks step `name` as finished OK at the given attempt.
// Idempotent: calling twice is fine and keeps the earlier timestamp.
func (s *State) RecordSuccess(name string, attempt int, skipped bool) {
	if s.Steps == nil {
		s.Steps = make(map[string]Step)
	}
	existing, ok := s.Steps[name]
	finishedAt := existing.FinishedAt
	if !ok || existing.Status != StatusSuccess {
		finishedAt = time.Now().UTC().Format(time.RFC3339)
	}
	s.Steps[name] = Step{
		Status:     StatusSuccess,
		Attempt:    attempt,
		FinishedAt: finishedAt,
		Skipped:    skipped,
	}
}

// RecordFailure marks step `name` as failed; keeps retrying in a subsequent
// projctl up run.
func (s *State) RecordFailure(name string, attempt int, lastError string) {
	if s.Steps == nil {
		s.Steps = make(map[string]Step)
	}
	// Cap error message to keep state.json bounded.
	if len(lastError) > 2048 {
		lastError = lastError[:2048] + " …[truncated]"
	}
	s.Steps[name] = Step{
		Status:    StatusFailed,
		Attempt:   attempt,
		LastError: lastError,
	}
}

// IsSuccessful reports whether step `name` has already finished OK
// (including by LLM-fallback skip). projctl up uses this to skip re-runs.
func (s *State) IsSuccessful(name string) bool {
	if s == nil || s.Steps == nil {
		return false
	}
	step, ok := s.Steps[name]
	return ok && step.Status == StatusSuccess
}

// Heartbeat stamps LastHeartbeatAt to now (UTC, RFC3339).
func (s *State) Heartbeat() {
	s.LastHeartbeatAt = time.Now().UTC().Format(time.RFC3339)
}

func fresh(guideVersion string) *State {
	return &State{
		GuideVersion: guideVersion,
		Steps:        make(map[string]Step),
	}
}
