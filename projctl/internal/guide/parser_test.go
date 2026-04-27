package guide

import (
	"strings"
	"testing"
)

const validGuide = "## install-php\n" +
	"\n" +
	"```projctl\n" +
	"cmd: composer install --no-interaction\n" +
	"cwd: /app\n" +
	"success_check: test -d /app/vendor\n" +
	"skippable: false\n" +
	"max_attempts: 3\n" +
	"retry_policy: exponential_backoff\n" +
	"timeout_seconds: 300\n" +
	"```\n" +
	"\n" +
	"Install PHP deps.\n" +
	"\n" +
	"## migrate\n" +
	"\n" +
	"```projctl\n" +
	"cmd: php artisan migrate --force\n" +
	"success_check: php artisan migrate:status | grep -q Ran\n" +
	"max_attempts: 2\n" +
	"```\n"

func TestParseValidGuide(t *testing.T) {
	g, err := Parse(strings.NewReader(validGuide))
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if len(g.Steps) != 2 {
		t.Fatalf("want 2 steps, got %d", len(g.Steps))
	}
	if g.Steps[0].Name != "install-php" {
		t.Errorf("first step name: %q", g.Steps[0].Name)
	}
	if g.Steps[0].MaxAttempts != 3 {
		t.Errorf("first step max_attempts: %d", g.Steps[0].MaxAttempts)
	}
	if g.Steps[1].Cwd != "/app" {
		t.Errorf("second step should default cwd to /app, got %q", g.Steps[1].Cwd)
	}
	if len(g.Version) != 64 {
		t.Errorf("version should be 64-char sha256 hex, got %d chars", len(g.Version))
	}
}

func TestParseEmptyIsError(t *testing.T) {
	_, err := Parse(strings.NewReader(""))
	if err == nil {
		t.Fatal("expected error for empty guide")
	}
}

func TestParseDuplicateStepName(t *testing.T) {
	dup := "## foo\n```projctl\ncmd: true\nsuccess_check: true\n```\n\n## foo\n```projctl\ncmd: true\nsuccess_check: true\n```\n"
	_, err := Parse(strings.NewReader(dup))
	if err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("expected duplicate-name error, got %v", err)
	}
}

func TestParseMissingSuccessCheck(t *testing.T) {
	bad := "## foo\n```projctl\ncmd: true\n```\n"
	_, err := Parse(strings.NewReader(bad))
	if err == nil || !strings.Contains(err.Error(), "success_check") {
		t.Fatalf("expected success_check error, got %v", err)
	}
}

func TestParseForbiddenDockerInCmd(t *testing.T) {
	bad := "## foo\n```projctl\ncmd: docker ps\nsuccess_check: true\n```\n"
	_, err := Parse(strings.NewReader(bad))
	if err == nil || !strings.Contains(err.Error(), "forbidden") {
		t.Fatalf("expected forbidden-pattern error, got %v", err)
	}
}

func TestParseForbiddenSecretInCmd(t *testing.T) {
	bad := "## foo\n```projctl\ncmd: API_TOKEN=abc echo hi\nsuccess_check: true\n```\n"
	_, err := Parse(strings.NewReader(bad))
	if err == nil || !strings.Contains(err.Error(), "forbidden") {
		t.Fatalf("expected forbidden-pattern error for inline secret, got %v", err)
	}
}

func TestParseMaxAttemptsCap(t *testing.T) {
	bad := "## foo\n```projctl\ncmd: true\nsuccess_check: true\nmax_attempts: 99\n```\n"
	_, err := Parse(strings.NewReader(bad))
	if err == nil || !strings.Contains(err.Error(), "max_attempts") {
		t.Fatalf("expected max_attempts cap error, got %v", err)
	}
}

func TestParseHashIsStable(t *testing.T) {
	g1, err := Parse(strings.NewReader(validGuide))
	if err != nil {
		t.Fatal(err)
	}
	g2, err := Parse(strings.NewReader(validGuide))
	if err != nil {
		t.Fatal(err)
	}
	if g1.Version != g2.Version {
		t.Fatal("parse must be deterministic: hash mismatch across identical inputs")
	}

	// Modifying the text changes the hash → state.json invalidation works.
	g3, err := Parse(strings.NewReader(validGuide + "\n## extra\n```projctl\ncmd: true\nsuccess_check: true\n```\n"))
	if err != nil {
		t.Fatal(err)
	}
	if g3.Version == g1.Version {
		t.Fatal("adding a step must change the guide hash")
	}
}
