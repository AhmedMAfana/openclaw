package steps

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestEmitterFillsDefaults(t *testing.T) {
	var buf bytes.Buffer
	e := NewEmitter(&buf, "inst-0123456789abcd", "0.1.0-dev")
	if err := e.StepStart("install-php"); err != nil {
		t.Fatal(err)
	}
	line := strings.TrimSpace(buf.String())
	var ev map[string]any
	if err := json.Unmarshal([]byte(line), &ev); err != nil {
		t.Fatalf("emitted JSON must parse: %v\n%s", err, line)
	}
	if ev["event"] != "step_start" {
		t.Errorf("event: %v", ev["event"])
	}
	if ev["step"] != "install-php" {
		t.Errorf("step: %v", ev["step"])
	}
	if ev["projctl_version"] != "0.1.0-dev" {
		t.Errorf("version: %v", ev["projctl_version"])
	}
	if ev["instance_slug"] != "inst-0123456789abcd" {
		t.Errorf("slug: %v", ev["instance_slug"])
	}
	if ev["at"] == nil || ev["at"] == "" {
		t.Error("at timestamp must be auto-filled")
	}
}

func TestEmitterStepSuccessHasExitCodeZero(t *testing.T) {
	var buf bytes.Buffer
	e := NewEmitter(&buf, "inst-0123456789abcd", "test")
	if err := e.StepSuccess("foo", 2); err != nil {
		t.Fatal(err)
	}
	var ev map[string]any
	_ = json.Unmarshal(buf.Bytes(), &ev)
	if ev["exit_code"].(float64) != 0 {
		t.Errorf("step_success must carry exit_code=0 per schema, got %v", ev["exit_code"])
	}
	if ev["attempt"].(float64) != 2 {
		t.Errorf("attempt: %v", ev["attempt"])
	}
}

func TestEmitterStepFailureHasNonzeroExitCode(t *testing.T) {
	var buf bytes.Buffer
	e := NewEmitter(&buf, "inst-0123456789abcd", "test")
	if err := e.StepFailure("foo", 1, 137); err != nil {
		t.Fatal(err)
	}
	var ev map[string]any
	_ = json.Unmarshal(buf.Bytes(), &ev)
	if ev["exit_code"].(float64) != 137 {
		t.Errorf("step_failure must carry non-zero exit_code, got %v", ev["exit_code"])
	}
}

func TestEmitterFatalIncludesReason(t *testing.T) {
	var buf bytes.Buffer
	e := NewEmitter(&buf, "", "test")
	if err := e.Fatal("parse guide.md: no steps"); err != nil {
		t.Fatal(err)
	}
	var ev map[string]any
	_ = json.Unmarshal(buf.Bytes(), &ev)
	if ev["event"] != "fatal" {
		t.Errorf("event: %v", ev["event"])
	}
	if ev["fatal_reason"] != "parse guide.md: no steps" {
		t.Errorf("fatal_reason: %v", ev["fatal_reason"])
	}
	// fatal may be emitted before slug is known.
	if _, ok := ev["instance_slug"]; ok && ev["instance_slug"] != "" {
		t.Errorf("slug should be omitted when unknown, got %v", ev["instance_slug"])
	}
}
