// Package main is the projctl entrypoint.
//
// projctl runs declarative guide.md steps inside a per-chat instance container.
// It is the deterministic counterpart to the orchestrator's LLM fallback:
// every step succeeds or fails on its own; LLM help is requested only
// through `projctl explain`. See specs/001-per-chat-instances/contracts/
// for the stdout JSON-line schema and the LLM-fallback envelope schema.
//
// Subcommands (to be implemented per tasks.md T023..T029):
//   up              run guide.md steps, resumable via state.json
//   doctor          health-check the instance
//   down            graceful stop
//   step <name>     re-run a specific step (with --retry)
//   explain         build a redacted envelope and request LLM help
//   heartbeat       daemon loop posting activity to the orchestrator
//   rotate-git-token receive a fresh GitHub installation token
//
// This file is a stub; full dispatch lands with T023.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/tagh-dev/projctl/internal/steps"
)

var version = "0.1.0-dev"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]
	switch cmd {
	case "--version", "-v":
		fmt.Println(version)
		return
	case "up":
		os.Exit(runUp(os.Args[2:]))
	case "doctor":
		os.Exit(runDoctor(os.Args[2:]))
	case "down":
		os.Exit(runDown(os.Args[2:]))
	case "heartbeat":
		os.Exit(runHeartbeat(os.Args[2:]))
	case "rotate-git-token":
		os.Exit(runRotateGitToken(os.Args[2:]))
	case "step", "explain":
		fmt.Fprintf(os.Stderr,
			"projctl: %q as a direct subcommand is not yet implemented "+
				"(T078's explain flow is invoked automatically by `projctl up`)\n",
			cmd)
		os.Exit(2)
	default:
		usage()
		os.Exit(2)
	}
}

func runUp(args []string) int {
	fs := flag.NewFlagSet("up", flag.ExitOnError)
	guidePath := fs.String("guide", "/app/guide.md", "path to guide.md")
	statePath := fs.String("state", "/var/lib/projctl/state.json", "path to state.json")
	slug := fs.String("slug", os.Getenv("INSTANCE_SLUG"), "instance slug (INSTANCE_SLUG env)")
	explainURL := fs.String("explain-url", os.Getenv("EXPLAIN_URL"), "POST URL for LLM fallback (EXPLAIN_URL env)")
	projectName := fs.String("project", os.Getenv("PROJECT_NAME"), "project name (PROJECT_NAME env)")
	_ = fs.Parse(args)

	em := steps.DefaultEmitter(*slug, version)

	// T078: bind the LLM fallback only when we have the URL + secret.
	// Without them we fall through to terminal failure after
	// max_attempts — same as the pre-T078 behaviour.
	var fallback steps.LLMFallback
	secret := os.Getenv("HEARTBEAT_SECRET")
	if *explainURL != "" && secret != "" {
		fallback = steps.MakeLLMFallback(steps.ExplainOptions{
			Slug:        *slug,
			ProjectName: *projectName,
			URL:         *explainURL,
			Secret:      secret,
			Version:     version,
		})
	}

	err := steps.Up(context.Background(), steps.UpOptions{
		GuidePath:    *guidePath,
		StatePath:    *statePath,
		InstanceSlug: *slug,
		Emitter:      em,
		Runner:       steps.ExecRunner{},
		LLMFallback:  fallback,
	})
	if err != nil {
		_ = em.Fatal(err.Error())
		return 1
	}
	return 0
}

// runHeartbeat — T052 daemon subcommand.
//
// Blocks until SIGINT/SIGTERM OR a fatal auth error (401/404/409) at
// which point projctl exits and compose supervises the restart.
func runHeartbeat(args []string) int {
	fs := flag.NewFlagSet("heartbeat", flag.ExitOnError)
	slug := fs.String("slug", os.Getenv("INSTANCE_SLUG"), "instance slug")
	url := fs.String("url", os.Getenv("HEARTBEAT_URL"), "heartbeat endpoint URL")
	interval := fs.Duration("interval", 0, "heartbeat interval (default 60s)")
	_ = fs.Parse(args)

	secret := os.Getenv("HEARTBEAT_SECRET")
	if *slug == "" || *url == "" || secret == "" {
		fmt.Fprintln(os.Stderr, "heartbeat: --slug, --url, and HEARTBEAT_SECRET required")
		return 2
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	err := steps.HeartbeatLoop(ctx, steps.HeartbeatOptions{
		Slug:     *slug,
		URL:      *url,
		Secret:   secret,
		Interval: *interval,
		Version:  version,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		return 1
	}
	return 0
}

// runRotateGitToken — T065 daemon subcommand.
//
// Performs an immediate rotation on startup (so the first git push
// has a working credential) then sleeps for the configured interval
// before the next rotation.
func runRotateGitToken(args []string) int {
	fs := flag.NewFlagSet("rotate-git-token", flag.ExitOnError)
	slug := fs.String("slug", os.Getenv("INSTANCE_SLUG"), "instance slug")
	url := fs.String("url", os.Getenv("ROTATE_GIT_TOKEN_URL"), "rotate-git-token endpoint URL")
	interval := fs.Duration("interval", 0, "rotation interval (default 45m)")
	home := fs.String("home", os.Getenv("HOME"), "home directory for .git-credentials")
	_ = fs.Parse(args)

	secret := os.Getenv("HEARTBEAT_SECRET")
	if *slug == "" || *url == "" || secret == "" {
		fmt.Fprintln(os.Stderr, "rotate-git-token: --slug, --url, and HEARTBEAT_SECRET required")
		return 2
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	err := steps.RotateGitTokenLoop(ctx, steps.RotateOptions{
		Slug:     *slug,
		URL:      *url,
		Secret:   secret,
		Interval: *interval,
		HomeDir:  *home,
		Version:  version,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		return 1
	}
	return 0
}

func runDoctor(args []string) int {
	fs := flag.NewFlagSet("doctor", flag.ExitOnError)
	guide := fs.String("guide", "/app/guide.md", "path to guide.md")
	statePath := fs.String("state", "/var/lib/projctl/state.json", "path to state.json")
	slug := fs.String("slug", os.Getenv("INSTANCE_SLUG"), "instance slug")
	_ = fs.Parse(args)

	em := steps.DefaultEmitter(*slug, version)
	if err := steps.Doctor(context.Background(), steps.DoctorOptions{
		GuidePath:    *guide,
		StatePath:    *statePath,
		InstanceSlug: *slug,
		Emitter:      em,
	}); err != nil {
		_ = em.Fatal(err.Error())
		return 1
	}
	return 0
}

func runDown(args []string) int {
	fs := flag.NewFlagSet("down", flag.ExitOnError)
	slug := fs.String("slug", os.Getenv("INSTANCE_SLUG"), "instance slug")
	_ = fs.Parse(args)

	em := steps.DefaultEmitter(*slug, version)
	if err := steps.Down(context.Background(), steps.DownOptions{
		InstanceSlug: *slug,
		Emitter:      em,
		Runner:       steps.ExecRunner{},
	}); err != nil {
		_ = em.Fatal(err.Error())
		return 1
	}
	return 0
}

func usage() {
	fmt.Fprint(os.Stderr, `projctl — TAGH Dev instance step runner

usage: projctl <command> [args]

commands:
  up                  run guide.md steps (resumable)
  doctor              health checks
  down                graceful stop
  step <name>         re-run a specific step
  explain             request LLM help for a failed step
  heartbeat           activity daemon
  rotate-git-token    receive a fresh GitHub token
  --version           print version

See specs/001-per-chat-instances/contracts/ for the stdout and envelope schemas.
`)
}
