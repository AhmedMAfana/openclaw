// File: explain.go — LLM fallback HTTP client + action applier.
//
// Spec: specs/001-per-chat-instances/contracts/llm-fallback-envelope.schema.json
//       + /internal/instances/<slug>/explain (orchestrator T079).
//
// Used by `projctl up` when a step exhausts its max_attempts retry
// budget. The flow:
//
//   1. Build the envelope (bounded, redacted — the orchestrator re-
//      redacts, but local redaction reduces the blast radius if the
//      HTTPS leg is intercepted).
//   2. POST it with HMAC-SHA256 over the raw body.
//   3. Parse {action, payload, reason}.
//   4. Apply the action or return the structured response to the
//      caller (up.go's LLMFallback wiring).
//
// The applier implements exactly four actions per arch §9:
//   shell_cmd — run payload before retrying the step cmd
//   patch     — git apply --check then git apply; retry cmd
//   skip      — only if the step is marked skippable
//   give_up   — emit fatal + exit non-zero
package steps

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/tagh-dev/projctl/internal/guide"
)

// ExplainOptions — `projctl explain` config.
type ExplainOptions struct {
	Slug        string
	ProjectName string
	URL         string
	Secret      string
	HTTPClient  *http.Client
	Version     string
}

// Envelope matches contracts/llm-fallback-envelope.schema.json.
// Field names MUST match the JSON Schema byte-for-byte — T077's
// contract test validates against it.
type Envelope struct {
	InstanceSlug     string       `json:"instance_slug"`
	ProjectName      string       `json:"project_name"`
	Step             EnvelopeStep `json:"step"`
	ExitCode         int          `json:"exit_code"`
	StdoutTail       string       `json:"stdout_tail"`
	StderrTail       string       `json:"stderr_tail"`
	GuideSection     string       `json:"guide_section"`
	PreviousAttempts int          `json:"previous_attempts"`
}

// EnvelopeStep matches the ``step`` sub-object in the schema.
type EnvelopeStep struct {
	Name         string `json:"name"`
	Cmd          string `json:"cmd"`
	Cwd          string `json:"cwd"`
	SuccessCheck string `json:"success_check,omitempty"`
	Skippable    bool   `json:"skippable"`
}

// ExplainResponse — the orchestrator's structured reply.
type ExplainResponse struct {
	Action  string `json:"action"`  // shell_cmd | patch | skip | give_up
	Payload string `json:"payload,omitempty"`
	Reason  string `json:"reason,omitempty"`
}

// Schema caps. Matches maxLength in the JSON Schema. The endpoint
// will re-truncate; local truncation keeps the blast radius small.
const (
	envelopeStdoutCap = 32 * 1024
	envelopeStderrCap = 32 * 1024
	envelopeGuideCap  = 16 * 1024
)

// Explain POSTs one envelope and returns the parsed response.
// Caller (up.go's LLMFallback) is responsible for applying the action.
func Explain(
	ctx context.Context,
	opts ExplainOptions,
	step guide.Step,
	exitCode int,
	stdoutTail, stderrTail, guideSection string,
	previousAttempts int,
) (*ExplainResponse, error) {
	if opts.Slug == "" || opts.URL == "" || opts.Secret == "" {
		return nil, fmt.Errorf("explain: slug/url/secret all required")
	}
	if opts.HTTPClient == nil {
		opts.HTTPClient = &http.Client{Timeout: 60 * time.Second}
	}

	env := Envelope{
		InstanceSlug: opts.Slug,
		ProjectName:  opts.ProjectName,
		Step: EnvelopeStep{
			Name:         step.Name,
			Cmd:          step.Cmd,
			Cwd:          step.Cwd,
			SuccessCheck: step.SuccessCheck,
			Skippable:    step.Skippable,
		},
		ExitCode:         exitCode,
		StdoutTail:       capString(stdoutTail, envelopeStdoutCap),
		StderrTail:       capString(stderrTail, envelopeStderrCap),
		GuideSection:     capString(guideSection, envelopeGuideCap),
		PreviousAttempts: clampInt(previousAttempts, 0, 3),
	}

	raw, err := json.Marshal(env)
	if err != nil {
		return nil, fmt.Errorf("marshal envelope: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, "POST", opts.URL, bytes.NewReader(raw))
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Signature", signBody(opts.Secret, raw))
	if opts.Version != "" {
		req.Header.Set("X-Projctl-Version", opts.Version)
	}

	resp, err := opts.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("explain: HTTP %d: %s",
			resp.StatusCode, truncate(string(body), 500))
	}

	var parsed ExplainResponse
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("parse response: %w", err)
	}

	// Normalise action — an unknown value degrades to give_up so the
	// caller never has to guess. The orchestrator already filters
	// invalid actions but this is defence-in-depth.
	switch parsed.Action {
	case "shell_cmd", "patch", "skip", "give_up":
		// OK
	default:
		parsed.Action = "give_up"
		if parsed.Reason == "" {
			parsed.Reason = "LLM returned unknown action; treating as give_up"
		}
	}
	return &parsed, nil
}

// MakeLLMFallback wraps Explain() into the LLMFallback signature expected
// by up.go's UpOptions. Wiring point from cmd/projctl/main.go — the
// fallback is only bound when HEARTBEAT_URL / HEARTBEAT_SECRET are set
// (i.e. projctl is running inside an orchestrated instance).
func MakeLLMFallback(opts ExplainOptions) LLMFallback {
	return func(
		ctx context.Context,
		step guide.Step,
		stdoutTail, stderrTail string,
		attempt int,
	) (*LLMActionInfo, error) {
		resp, err := Explain(
			ctx, opts, step,
			/*exitCode*/ 1,
			stdoutTail, stderrTail,
			/*guideSection*/ step.Description,
			/*previousAttempts*/ attempt-1,
		)
		if err != nil {
			return &LLMActionInfo{
				Action: "give_up",
				Reason: fmt.Sprintf("explain endpoint unreachable: %v", err),
			}, nil
		}
		return &LLMActionInfo{
			Action:  resp.Action,
			Payload: resp.Payload,
			Reason:  resp.Reason,
		}, nil
	}
}

// --- helpers ---------------------------------------------------------

func capString(s string, max int) string {
	if len(s) <= max {
		return s
	}
	marker := fmt.Sprintf("\n... %d chars truncated ...\n", len(s)-max)
	return marker + s[len(s)-max:]
}

func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "..."
}
