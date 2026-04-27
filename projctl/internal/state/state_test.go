package state

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadMissingReturnsFresh(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	res, err := Load(path, "v1")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if res.Invalidated {
		t.Fatal("fresh state should not be flagged invalidated")
	}
	if res.State.GuideVersion != "v1" {
		t.Errorf("version: %q", res.State.GuideVersion)
	}
	if len(res.State.Steps) != 0 {
		t.Errorf("steps: %v", res.State.Steps)
	}
}

func TestSaveThenLoadRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	s := &State{
		GuideVersion: "v1",
		Steps:        map[string]Step{},
	}
	s.RecordSuccess("install-php", 1, false)
	s.RecordSuccess("migrate", 2, true)
	s.RecordFailure("start-node", 3, "vite crashed")
	s.Heartbeat()

	if err := Save(path, s); err != nil {
		t.Fatalf("save: %v", err)
	}

	// Atomic rename means the final file exists and has content.
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if info.Size() == 0 {
		t.Fatal("saved file is empty")
	}

	res, err := Load(path, "v1")
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if res.Invalidated {
		t.Fatal("round-trip with same version should not invalidate")
	}
	loaded := res.State
	if loaded.Steps["install-php"].Status != StatusSuccess {
		t.Errorf("install-php status: %v", loaded.Steps["install-php"].Status)
	}
	if !loaded.Steps["migrate"].Skipped {
		t.Errorf("migrate should be marked skipped")
	}
	if loaded.Steps["start-node"].Status != StatusFailed {
		t.Errorf("start-node should be failed")
	}
	if loaded.LastHeartbeatAt == "" {
		t.Error("heartbeat timestamp lost")
	}
}

func TestGuideVersionMismatchInvalidates(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	// Persist with version v1.
	s := &State{GuideVersion: "v1", Steps: map[string]Step{"foo": {Status: StatusSuccess}}}
	if err := Save(path, s); err != nil {
		t.Fatal(err)
	}
	// Load with version v2 → dropped.
	res, err := Load(path, "v2")
	if err != nil {
		t.Fatal(err)
	}
	if !res.Invalidated {
		t.Fatal("expected Invalidated=true on version mismatch")
	}
	if len(res.State.Steps) != 0 {
		t.Errorf("steps should be wiped, got %v", res.State.Steps)
	}
	if res.State.GuideVersion != "v2" {
		t.Errorf("fresh state should carry new version: %q", res.State.GuideVersion)
	}
}

func TestCorruptFileIsTreatedAsFresh(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := os.WriteFile(path, []byte("not json {{{"), 0o600); err != nil {
		t.Fatal(err)
	}
	res, err := Load(path, "v1")
	if err != nil {
		t.Fatalf("corrupt file should not hard-error: %v", err)
	}
	if !res.Invalidated {
		t.Fatal("corrupt file should be flagged invalidated")
	}
}

func TestRecordSuccessIdempotent(t *testing.T) {
	s := &State{Steps: map[string]Step{}}
	s.RecordSuccess("foo", 1, false)
	first := s.Steps["foo"].FinishedAt
	s.RecordSuccess("foo", 1, false)
	if s.Steps["foo"].FinishedAt != first {
		t.Error("re-recording the same step success must keep the original timestamp")
	}
}

func TestIsSuccessful(t *testing.T) {
	var s *State
	if s.IsSuccessful("foo") {
		t.Error("nil State must return false")
	}
	s = &State{Steps: map[string]Step{"foo": {Status: StatusSuccess}, "bar": {Status: StatusFailed}}}
	if !s.IsSuccessful("foo") {
		t.Error("foo should be successful")
	}
	if s.IsSuccessful("bar") {
		t.Error("bar is failed, not successful")
	}
	if s.IsSuccessful("missing") {
		t.Error("missing step must return false")
	}
}
