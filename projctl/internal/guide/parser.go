// Package guide parses a guide.md file into a deterministic step list.
//
// Format spec: src/taghdev/setup/compose_templates/GUIDE_SPEC.md.
// Each step is a `## <name>` heading followed by a ```projctl fenced block
// carrying the step's metadata as key: value pairs.
//
// Parse errors are fatal — projctl emits a `fatal` JSON-line event with
// fatal_reason set to the parse failure reason (contracts/projctl-stdout.schema.json).
package guide

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"regexp"
	"strconv"
	"strings"
)

// Step is one parsed `## name` section of guide.md.
type Step struct {
	Name           string
	Cmd            string
	Cwd            string
	SuccessCheck   string
	Skippable      bool
	RetryPolicy    string // "none" | "fixed_delay" | "exponential_backoff"
	MaxAttempts    int
	TimeoutSeconds int
	// Description is the Markdown prose that follows the projctl block;
	// projctl explain ships this verbatim in the LLM envelope.
	Description string
}

// Guide is the whole parsed file plus a content hash.
type Guide struct {
	// SHA256 of the raw file. state.json compares against this; a mismatch
	// invalidates all recorded step outcomes (GUIDE_SPEC.md §5).
	Version string
	Steps   []Step
}

// Defaults mirror GUIDE_SPEC.md §2 field table.
const (
	defaultCwd         = "/app"
	defaultRetryPolicy = "none"
	defaultMaxAttempts = 1
	defaultTimeoutSecs = 300
	maxAttemptsCap     = 5
)

var (
	headingRE = regexp.MustCompile(`^##\s+([a-z][a-z0-9\-]*)\s*$`)
	fenceOpen = regexp.MustCompile("^```projctl\\s*$")
	fenceAny  = regexp.MustCompile("^```")
	kvRE      = regexp.MustCompile(`^([a-z_]+)\s*:\s*(.+?)\s*$`)
)

// ParseFile reads path and returns a Guide. Errors are parse-level;
// caller should emit a fatal event and exit.
func ParseFile(path string) (*Guide, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()
	return Parse(f)
}

// Parse reads from r and returns a Guide.
//
// Grammar (informal):
//
//	## name
//	```projctl
//	key: value
//	...
//	```
//	<description paragraphs until the next `## ` or EOF>
//
// Rules enforced here (failures = fatal parse errors):
//   - heading name is kebab-case, unique
//   - every step has a projctl fence
//   - required keys: cmd, success_check
//   - max_attempts capped at 5
//   - forbidden patterns in cmd (docker, sudo, etc.) per GUIDE_SPEC.md §7
func Parse(r io.Reader) (*Guide, error) {
	buf, err := io.ReadAll(r)
	if err != nil {
		return nil, fmt.Errorf("read: %w", err)
	}
	hash := sha256.Sum256(buf)

	// Pre-tokenise into a slice so the per-step body parser can stop
	// at a `## ` boundary without losing the boundary line. The earlier
	// bufio-based design dropped every other step (the `##` consumed by
	// parseStepBody never re-surfaced in the outer loop). Indexing a
	// slice is the simplest fix; `lines[i]` can be re-read freely.
	lines := strings.Split(string(buf), "\n")
	// Trim a trailing empty line introduced by a final newline.
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}

	var steps []Step
	seen := make(map[string]bool)

	i := 0
	for i < len(lines) {
		text := lines[i]
		m := headingRE.FindStringSubmatch(text)
		if m == nil {
			i++
			continue
		}
		name := m[1]
		lineNo := i + 1
		if seen[name] {
			return nil, fmt.Errorf("line %d: duplicate step name %q", lineNo, name)
		}
		seen[name] = true

		// parseStepBodySlice consumes from i+1 up to but not including the
		// next `## ` heading (or end of file). It returns the index of the
		// next line to inspect, so the outer loop sees the next heading.
		step, next, err := parseStepBodySlice(lines, i+1, name)
		if err != nil {
			return nil, err
		}
		steps = append(steps, step)
		i = next
	}

	if len(steps) == 0 {
		return nil, fmt.Errorf("guide has no steps (no `## name` headings found)")
	}

	return &Guide{
		Version: hex.EncodeToString(hash[:]),
		Steps:   steps,
	}, nil
}

// parseStepBodySlice replaces the bufio-pushback dance in parseStepBody.
// It walks `lines` starting at `start`, returning the populated Step and
// the index of the line where parsing should resume — either the next
// `## ` heading or len(lines).
func parseStepBodySlice(lines []string, start int, name string) (Step, int, error) {
	step := Step{
		Name:           name,
		Cwd:            defaultCwd,
		RetryPolicy:    defaultRetryPolicy,
		MaxAttempts:    defaultMaxAttempts,
		TimeoutSeconds: defaultTimeoutSecs,
	}

	inFence := false
	fenceSeen := false
	var descLines []string

	i := start
	for i < len(lines) {
		text := lines[i]
		lineNo := i + 1

		// A new `## ` heading ends this step's body. Return the index
		// pointing AT the heading so the outer loop sees it.
		if !inFence && strings.HasPrefix(text, "## ") {
			break
		}

		if !inFence && fenceOpen.MatchString(text) {
			inFence = true
			fenceSeen = true
			i++
			continue
		}
		if inFence {
			if fenceAny.MatchString(text) {
				inFence = false
				i++
				continue
			}
			trimmed := strings.TrimSpace(text)
			if trimmed == "" || strings.HasPrefix(trimmed, "#") {
				i++
				continue
			}
			m := kvRE.FindStringSubmatch(text)
			if m == nil {
				return step, i, fmt.Errorf(
					"line %d: step %q has a non-kv line inside projctl fence: %q",
					lineNo, name, text,
				)
			}
			if err := applyKey(&step, m[1], m[2]); err != nil {
				return step, i, fmt.Errorf("line %d: step %q: %w", lineNo, name, err)
			}
			i++
			continue
		}

		descLines = append(descLines, text)
		i++
	}

	if !fenceSeen {
		return step, i, fmt.Errorf(
			"step %q is missing a ```projctl fenced block", name,
		)
	}
	if err := validate(&step); err != nil {
		return step, i, err
	}
	step.Description = strings.TrimSpace(strings.Join(descLines, "\n"))
	return step, i, nil
}

// (Old `parseStepBody` removed — it had a known bufio-pushback bug that
// dropped every other step from a multi-step guide. Replaced by
// parseStepBodySlice above.)

func applyKey(s *Step, key, val string) error {
	// Trim surrounding quotes if present so `cmd: "echo hi"` works.
	if len(val) >= 2 && val[0] == '"' && val[len(val)-1] == '"' {
		val = val[1 : len(val)-1]
	}
	switch key {
	case "cmd":
		s.Cmd = val
	case "cwd":
		s.Cwd = val
	case "success_check":
		s.SuccessCheck = val
	case "skippable":
		b, err := strconv.ParseBool(val)
		if err != nil {
			return fmt.Errorf("skippable must be true/false: %q", val)
		}
		s.Skippable = b
	case "retry_policy":
		switch val {
		case "none", "fixed_delay", "exponential_backoff":
			s.RetryPolicy = val
		default:
			return fmt.Errorf("retry_policy must be none|fixed_delay|exponential_backoff, got %q", val)
		}
	case "max_attempts":
		n, err := strconv.Atoi(val)
		if err != nil {
			return fmt.Errorf("max_attempts must be int: %q", val)
		}
		if n < 1 || n > maxAttemptsCap {
			return fmt.Errorf("max_attempts must be 1..%d, got %d", maxAttemptsCap, n)
		}
		s.MaxAttempts = n
	case "timeout_seconds":
		n, err := strconv.Atoi(val)
		if err != nil {
			return fmt.Errorf("timeout_seconds must be int: %q", val)
		}
		if n < 1 {
			return fmt.Errorf("timeout_seconds must be >= 1, got %d", n)
		}
		s.TimeoutSeconds = n
	default:
		return fmt.Errorf("unknown key %q (allowed: cmd, cwd, success_check, skippable, retry_policy, max_attempts, timeout_seconds)", key)
	}
	return nil
}

// Forbidden command patterns per GUIDE_SPEC.md §7.
var forbiddenCmdPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)\bdocker\b`),
	regexp.MustCompile(`(?i)\bkubectl\b`),
	regexp.MustCompile(`(?i)\bsudo\b`),
	regexp.MustCompile(`(?i)\bsu\s+-\b`),
	// Secret-looking env inlined into cmd.
	regexp.MustCompile(`(?i)(SECRET|TOKEN|PASSWORD|KEY|AUTH)[A-Z0-9_]*\s*=\s*\S+`),
}

func validate(s *Step) error {
	if s.Cmd == "" {
		return fmt.Errorf("step %q: cmd is required", s.Name)
	}
	if s.SuccessCheck == "" {
		return fmt.Errorf("step %q: success_check is required", s.Name)
	}
	for _, re := range forbiddenCmdPatterns {
		if re.MatchString(s.Cmd) {
			return fmt.Errorf(
				"step %q: forbidden pattern in cmd (GUIDE_SPEC.md §7); rewrite the step",
				s.Name,
			)
		}
	}
	return nil
}
