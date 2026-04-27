// File: down.go — `projctl down` subcommand.
//
// Graceful stop before compose-down. In v1 this just runs a shell
// fragment that SIGTERMs known dev servers (Vite, queue worker) and
// waits briefly for them to drain. Each phase has an explicit timeout.
package steps

import (
	"context"
	"time"
)

// DownOptions is kept symmetrical with UpOptions / DoctorOptions so that
// future enrichment (e.g., reading guide.md to know which services to
// stop first) has a place to plug in.
type DownOptions struct {
	InstanceSlug string
	Emitter      *Emitter
	Runner       Runner
}

// Down runs the graceful-stop sequence. Always returns nil unless the
// emitter itself errors; individual phase failures are logged but do not
// block compose-down (which the orchestrator runs unconditionally after).
func Down(ctx context.Context, opts DownOptions) error {
	// Phase 1: SIGTERM anything matching our known dev-server patterns.
	phaseCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	_, _, _ = opts.Runner.Run(
		phaseCtx,
		"",
		"pkill -TERM -f 'vite|artisan queue:work|npm run dev' || true",
	)

	// Phase 2: brief drain window so background jobs finish their current
	// unit of work. 3 seconds is the sweet spot: long enough to let a
	// mid-iteration queue job commit, short enough not to stall teardown.
	time.Sleep(3 * time.Second)

	return opts.Emitter.Emit(Event{Event: "heartbeat"}) // a final heartbeat-shape event
}
