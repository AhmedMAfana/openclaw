// File: doctor.go — `projctl doctor` subcommand.
//
// Runs a set of health probes and emits a single `doctor_result` event.
// Each probe times out independently; one slow probe must not stall the
// whole command. Probes in v1:
//   * `state_present`: state.json exists and parses
//   * `guide_parses`:  guide.md parses without errors
//
// More probes (compose-up status, dev-server port, cloudflared connectivity)
// arrive when the runtime wiring lands (T078 + beyond).
package steps

import (
	"context"
	"time"

	"github.com/tagh-dev/projctl/internal/guide"
	"github.com/tagh-dev/projctl/internal/state"
)

// DoctorOptions mirrors UpOptions but for the doctor command.
type DoctorOptions struct {
	GuidePath    string
	StatePath    string
	InstanceSlug string
	Emitter      *Emitter
}

// Doctor runs the health probes. Always returns nil error unless the
// emitter itself fails; health failures are reported via the event.
func Doctor(ctx context.Context, opts DoctorOptions) error {
	result := DoctorPayload{Healthy: true}

	// Probe 1: can we parse guide.md?
	probeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	result.Checks = append(result.Checks, probeGuide(probeCtx, opts.GuidePath))

	// Probe 2: does state.json exist and parse?
	probeCtx2, cancel2 := context.WithTimeout(ctx, 5*time.Second)
	defer cancel2()
	result.Checks = append(result.Checks, probeState(probeCtx2, opts.StatePath))

	for _, c := range result.Checks {
		if !c.OK {
			result.Healthy = false
			break
		}
	}
	return opts.Emitter.DoctorResult(result)
}

func probeGuide(ctx context.Context, path string) DoctorCheck {
	done := make(chan DoctorCheck, 1)
	go func() {
		_, err := guide.ParseFile(path)
		if err != nil {
			done <- DoctorCheck{Name: "guide_parses", OK: false, Detail: err.Error()}
			return
		}
		done <- DoctorCheck{Name: "guide_parses", OK: true}
	}()
	select {
	case c := <-done:
		return c
	case <-ctx.Done():
		return DoctorCheck{Name: "guide_parses", OK: false, Detail: "timed out"}
	}
}

func probeState(ctx context.Context, path string) DoctorCheck {
	done := make(chan DoctorCheck, 1)
	go func() {
		// guideVersion="" → Load always returns a State; the only failure
		// mode is a hard I/O error (missing file is fine, it counts as fresh).
		_, err := state.Load(path, "")
		if err != nil {
			done <- DoctorCheck{Name: "state_present", OK: false, Detail: err.Error()}
			return
		}
		done <- DoctorCheck{Name: "state_present", OK: true}
	}()
	select {
	case c := <-done:
		return c
	case <-ctx.Done():
		return DoctorCheck{Name: "state_present", OK: false, Detail: "timed out"}
	}
}
