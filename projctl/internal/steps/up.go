// File: up.go — `projctl up` subcommand.
//
// Executes every step in guide.md in order, honouring resume state.
// Stops at the first step that fails all retries + all LLM attempts
// (LLM is the `explain` codepath; skeleton call-out here, full wiring
// in T078).
package steps

import (
	"context"
	"fmt"
	"io"
	"math"
	"os/exec"
	"time"

	"github.com/tagh-dev/projctl/internal/guide"
	"github.com/tagh-dev/projctl/internal/state"
)

// UpOptions carries the inputs to Up. Kept explicit so tests can wire a
// fake runner without touching real subprocess / filesystem.
type UpOptions struct {
	GuidePath    string
	StatePath    string
	InstanceSlug string
	Emitter      *Emitter
	// Runner runs a shell command with a timeout and returns
	// (exitCode, stdoutTail, stderrTail). Injectable for testing.
	Runner Runner
	// LLMFallback is called when a step fails beyond max_attempts. The
	// returned action drives what projctl does next; if nil, no fallback
	// is attempted and the step is declared failed immediately.
	LLMFallback LLMFallback
}

// Runner executes one command and returns its outcome. Implementations:
//   - execRunner in this file (real sh -c <cmd>)
//   - a fake in projctl/tests for deterministic tests.
type Runner interface {
	Run(ctx context.Context, cwd, cmd string) (exitCode int, stdout, stderr string)
}

// LLMFallback returns the LLM's structured response for a failed step.
// When it returns action=="give_up", the step is failed terminally.
type LLMFallback func(ctx context.Context, step guide.Step, stdoutTail, stderrTail string, attempt int) (*LLMActionInfo, error)

// Up runs all steps. Returns an error for fatal conditions (parse failure,
// bad state file, etc.); step failures are reported via events and return
// a normal error indicating which step failed.
func Up(ctx context.Context, opts UpOptions) error {
	g, err := guide.ParseFile(opts.GuidePath)
	if err != nil {
		opts.Emitter.Fatal(fmt.Sprintf("parse guide.md: %v", err))
		return err
	}

	loaded, err := state.Load(opts.StatePath, g.Version)
	if err != nil {
		opts.Emitter.Fatal(fmt.Sprintf("load state: %v", err))
		return err
	}
	st := loaded.State

	for _, step := range g.Steps {
		if st.IsSuccessful(step.Name) {
			// Resumed: already done, skip silently per research.md §7.
			continue
		}
		if err := opts.Emitter.StepStart(step.Name); err != nil {
			return err
		}
		if err := runOneStep(ctx, step, st, opts); err != nil {
			_ = state.Save(opts.StatePath, st)
			return err
		}
		if err := state.Save(opts.StatePath, st); err != nil {
			return fmt.Errorf("save state after step %q: %w", step.Name, err)
		}
	}
	return nil
}

// runOneStep loops attempts + LLM fallback for one step.
func runOneStep(ctx context.Context, step guide.Step, st *state.State, opts UpOptions) error {
	const maxLLMAttempts = 3

	for attempt := 1; attempt <= step.MaxAttempts; attempt++ {
		exitCode, stdoutTail, stderrTail := runWithTimeout(ctx, step, opts.Runner)

		if exitCode == 0 {
			checkPassed := runSuccessCheck(ctx, step, opts.Runner)
			_ = opts.Emitter.SuccessCheck(step.Name, step.SuccessCheck, checkPassed)
			if checkPassed {
				_ = opts.Emitter.StepSuccess(step.Name, attempt)
				st.RecordSuccess(step.Name, attempt, false)
				return nil
			}
			// Cmd returned 0 but success_check failed — treat as failure.
			exitCode = 1
		}

		_ = opts.Emitter.StepFailure(step.Name, attempt, exitCode)
		st.RecordFailure(step.Name, attempt, stderrTail)

		if attempt < step.MaxAttempts {
			backoff(step.RetryPolicy, attempt)
			continue
		}

		// All retries exhausted → LLM fallback if configured.
		if opts.LLMFallback == nil {
			return fmt.Errorf("step %q failed after %d attempts", step.Name, attempt)
		}
		for lAttempt := 1; lAttempt <= maxLLMAttempts; lAttempt++ {
			action, err := opts.LLMFallback(ctx, step, stdoutTail, stderrTail, lAttempt)
			if err != nil {
				return fmt.Errorf("step %q: LLM fallback attempt %d failed: %w",
					step.Name, lAttempt, err)
			}
			_ = opts.Emitter.Emit(Event{
				Event:     "llm_action",
				Step:      step.Name,
				Attempt:   lAttempt,
				LLMAction: action,
			})
			switch action.Action {
			case "skip":
				if !step.Skippable {
					continue // model returned invalid action; retry
				}
				_ = opts.Emitter.StepSuccess(step.Name, attempt)
				st.RecordSuccess(step.Name, attempt, true)
				return nil
			case "give_up":
				return fmt.Errorf("step %q: LLM gave up: %s", step.Name, action.Reason)
			case "shell_cmd":
				// Run the suggested prefix cmd, then re-run the step's cmd.
				_, _, _ = opts.Runner.Run(ctx, step.Cwd, action.Payload)
				// Loop body re-runs step.Cmd on next iteration by falling
				// through; but to keep the loop simple we do an inline retry.
				ec, _, _ := opts.Runner.Run(ctx, step.Cwd, step.Cmd)
				if ec == 0 && runSuccessCheck(ctx, step, opts.Runner) {
					_ = opts.Emitter.StepSuccess(step.Name, attempt)
					st.RecordSuccess(step.Name, attempt, false)
					return nil
				}
			case "patch":
				// `git apply --check <patch>` gated elsewhere (T078);
				// in this skeleton we attempt and fall through.
				_, _, _ = opts.Runner.Run(ctx, step.Cwd, "git apply --check - <<'EOF'\n"+action.Payload+"\nEOF")
				_, _, _ = opts.Runner.Run(ctx, step.Cwd, "git apply - <<'EOF'\n"+action.Payload+"\nEOF")
				ec, _, _ := opts.Runner.Run(ctx, step.Cwd, step.Cmd)
				if ec == 0 && runSuccessCheck(ctx, step, opts.Runner) {
					_ = opts.Emitter.StepSuccess(step.Name, attempt)
					st.RecordSuccess(step.Name, attempt, false)
					return nil
				}
			default:
				// Malformed action — retry the LLM.
			}
		}
		return fmt.Errorf("step %q: LLM fallback exhausted after %d attempts", step.Name, maxLLMAttempts)
	}
	return fmt.Errorf("step %q: unreachable retry exit", step.Name)
}

// runWithTimeout wraps step execution in a context with step.TimeoutSeconds.
// Mandatory per Constitution Principle IX — no step ever runs unbounded.
func runWithTimeout(ctx context.Context, step guide.Step, r Runner) (int, string, string) {
	timeoutCtx, cancel := context.WithTimeout(ctx, time.Duration(step.TimeoutSeconds)*time.Second)
	defer cancel()
	return r.Run(timeoutCtx, step.Cwd, step.Cmd)
}

// runSuccessCheck runs the step's success_check under a short fixed timeout.
func runSuccessCheck(ctx context.Context, step guide.Step, r Runner) bool {
	const checkTimeout = 15 * time.Second
	timeoutCtx, cancel := context.WithTimeout(ctx, checkTimeout)
	defer cancel()
	ec, _, _ := r.Run(timeoutCtx, step.Cwd, step.SuccessCheck)
	return ec == 0
}

// backoff sleeps per retry_policy before the next attempt.
func backoff(policy string, prevAttempt int) {
	switch policy {
	case "fixed_delay":
		time.Sleep(2 * time.Second)
	case "exponential_backoff":
		// 2, 4, 8, 16, 32 seconds capped.
		delay := math.Min(32, math.Pow(2, float64(prevAttempt)))
		time.Sleep(time.Duration(delay) * time.Second)
	default: // "none"
		return
	}
}

// ExecRunner runs commands via /bin/sh -c with the provided context.
// This is the production Runner; tests inject their own.
type ExecRunner struct{}

// Run implements Runner.
func (ExecRunner) Run(ctx context.Context, cwd, cmd string) (int, string, string) {
	c := exec.CommandContext(ctx, "/bin/sh", "-c", cmd)
	if cwd != "" {
		c.Dir = cwd
	}
	stdout, _ := c.StdoutPipe()
	stderr, _ := c.StderrPipe()
	if err := c.Start(); err != nil {
		return 1, "", err.Error()
	}
	stdoutBytes, _ := io.ReadAll(stdout)
	stderrBytes, _ := io.ReadAll(stderr)
	_ = c.Wait()
	exitCode := c.ProcessState.ExitCode()
	return exitCode, tail(string(stdoutBytes), 200), tail(string(stderrBytes), 200)
}

// tail returns at most `maxLines` trailing lines of `s`, with a truncation
// marker at the head if lines were dropped. Used for the LLM envelope caps
// (contracts/llm-fallback-envelope.schema.json).
func tail(s string, maxLines int) string {
	if s == "" {
		return s
	}
	lines := splitLines(s)
	if len(lines) <= maxLines {
		return s
	}
	dropped := len(lines) - maxLines
	return fmt.Sprintf("... %d lines truncated ...\n", dropped) +
		joinLines(lines[dropped:])
}

func splitLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}

func joinLines(ls []string) string {
	var b []byte
	for i, l := range ls {
		if i > 0 {
			b = append(b, '\n')
		}
		b = append(b, l...)
	}
	return string(b)
}
