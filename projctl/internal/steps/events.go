// Package steps implements projctl's subcommands: up, doctor, down.
//
// All subcommands emit JSON-line events to stdout per the schema at
// specs/001-per-chat-instances/contracts/projctl-stdout.schema.json.
// One event per line; the orchestrator tails `docker compose logs` and
// parses each line.
package steps

import (
	"encoding/json"
	"io"
	"os"
	"time"
)

// Event is the line shape all subcommands emit. Fields are optional per
// the schema's `allOf` branches; we omit zero values on marshal.
type Event struct {
	Event           string          `json:"event"`
	At              string          `json:"at"`
	ProjctlVersion  string          `json:"projctl_version"`
	InstanceSlug    string          `json:"instance_slug,omitempty"`
	Step            string          `json:"step,omitempty"`
	Attempt         int             `json:"attempt,omitempty"`
	ExitCode        *int            `json:"exit_code,omitempty"`
	StdoutLine      string          `json:"stdout_line,omitempty"`
	StderrLine      string          `json:"stderr_line,omitempty"`
	SuccessCheckCmd string          `json:"success_check_cmd,omitempty"`
	CheckPassed     *bool           `json:"success_check_passed,omitempty"`
	LLMAction       *LLMActionInfo  `json:"llm_action,omitempty"`
	Doctor          *DoctorPayload  `json:"doctor,omitempty"`
	FatalReason     string          `json:"fatal_reason,omitempty"`
}

// LLMActionInfo is the structured shape the LLM fallback returns.
type LLMActionInfo struct {
	Action  string `json:"action"`  // shell_cmd | patch | skip | give_up
	Payload string `json:"payload,omitempty"`
	Reason  string `json:"reason,omitempty"`
}

// DoctorPayload is the structured shape of a `projctl doctor` result.
type DoctorPayload struct {
	Healthy bool          `json:"healthy"`
	Checks  []DoctorCheck `json:"checks"`
}

// DoctorCheck is one named health probe.
type DoctorCheck struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail,omitempty"`
}

// Emitter writes events as newline-delimited JSON. Thread-safe is NOT
// required — projctl is single-goroutine for subcommand execution.
type Emitter struct {
	w       io.Writer
	slug    string
	version string
}

// NewEmitter wraps an io.Writer (usually os.Stdout) for event output.
func NewEmitter(w io.Writer, instanceSlug, projctlVersion string) *Emitter {
	return &Emitter{w: w, slug: instanceSlug, version: projctlVersion}
}

// DefaultEmitter emits to os.Stdout.
func DefaultEmitter(instanceSlug, projctlVersion string) *Emitter {
	return NewEmitter(os.Stdout, instanceSlug, projctlVersion)
}

// Emit writes one event as JSON followed by \n.
func (e *Emitter) Emit(ev Event) error {
	if ev.At == "" {
		ev.At = time.Now().UTC().Format(time.RFC3339Nano)
	}
	if ev.ProjctlVersion == "" {
		ev.ProjctlVersion = e.version
	}
	if ev.InstanceSlug == "" && e.slug != "" {
		ev.InstanceSlug = e.slug
	}
	b, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	if _, err := e.w.Write(append(b, '\n')); err != nil {
		return err
	}
	return nil
}

// Helpers for the common event shapes.

func (e *Emitter) StepStart(name string) error {
	return e.Emit(Event{Event: "step_start", Step: name})
}

func (e *Emitter) StepSuccess(name string, attempt int) error {
	zero := 0
	return e.Emit(Event{
		Event:    "step_success",
		Step:     name,
		Attempt:  attempt,
		ExitCode: &zero,
	})
}

func (e *Emitter) StepFailure(name string, attempt, exitCode int) error {
	return e.Emit(Event{
		Event:    "step_failure",
		Step:     name,
		Attempt:  attempt,
		ExitCode: &exitCode,
	})
}

func (e *Emitter) SuccessCheck(name, cmd string, passed bool) error {
	return e.Emit(Event{
		Event:           "success_check",
		Step:            name,
		SuccessCheckCmd: cmd,
		CheckPassed:     &passed,
	})
}

func (e *Emitter) Heartbeat() error {
	return e.Emit(Event{Event: "heartbeat"})
}

func (e *Emitter) DoctorResult(p DoctorPayload) error {
	return e.Emit(Event{Event: "doctor_result", Doctor: &p})
}

func (e *Emitter) Fatal(reason string) error {
	return e.Emit(Event{Event: "fatal", FatalReason: reason})
}
