"""T014: redactor must mask every category Principle IV requires.

Categories (from spec.md FR-032/FR-033 + constitution.md IV):
  1. HTTP bearer tokens
  2. AWS access + secret access keys
  3. GCP / PEM private keys
  4. Cloudflare API tokens
  5. SSH private-key blocks
  6. .env-style KEY=VALUE where key matches /SECRET|TOKEN|PASSWORD|KEY|AUTH/i
  7. GitHub tokens (ghs_, ghp_, github_pat_)

Also: redact must be idempotent AND a byte-for-byte identity on non-secret text.
"""
from openclow.services.audit_service import redact


def test_bearer_token_masked():
    raw = "GET /foo\nAuthorization: Bearer abcdef.123456+qwe/uiop_1234=\n"
    out = redact(raw)
    assert "[REDACTED]" in out
    assert "abcdef.123456" not in out
    # Key prefix preserved so operators can tell there WAS a header.
    assert "Authorization: Bearer" in out


def test_aws_access_key_id_masked():
    raw = "error: credentials AKIAIOSFODNN7EXAMPLE expired"
    out = redact(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED]" in out


def test_aws_secret_access_key_masked():
    raw = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    out = redact(raw)
    assert "wJalrXUtnFEMI" not in out
    assert "aws_secret_access_key=[REDACTED]" in out.lower() or "[REDACTED]" in out


def test_pem_private_key_block_masked():
    raw = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCfake...\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    out = redact(raw)
    assert "MIIEvQIBADAN" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "[REDACTED]" in out
    # Non-secret context preserved.
    assert out.startswith("before")
    assert out.endswith("after")


def test_openssh_private_key_block_masked():
    raw = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAA\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    assert "b3BlbnNzaC1rZXk" not in redact(raw)


def test_cloudflare_api_token_masked():
    raw = "CF_API_TOKEN=1234567890abcdef1234567890abcdef"
    out = redact(raw)
    assert "1234567890abcdef" not in out
    assert "CF_API_TOKEN=" in out.replace("_", "_")  # prefix preserved


def test_github_installation_token_masked():
    raw = "git push -u origin main using token ghs_ABCdef1234567890abcdef1234567890abc"
    out = redact(raw)
    assert "ghs_ABCdef1234567890abcdef1234567890abc" not in out
    assert "[REDACTED]" in out


def test_github_pat_masked():
    raw = "export GITHUB_PAT=ghp_ABCdef1234567890ABCdef1234567890ABCde"
    # Two patterns could both match here: the generic KEY=VALUE and the GitHub
    # prefix. Either covering the secret is acceptable.
    out = redact(raw)
    assert "ghp_ABCdef1234567890ABCdef1234567890ABCde" not in out
    assert "[REDACTED]" in out


def test_generic_env_var_with_secret_name_masked():
    # Plan §Credential scoping: any KEY matching the env-var pattern is masked.
    for key in ("DB_PASSWORD", "HEARTBEAT_SECRET", "API_KEY", "AUTH_TOKEN", "JWT_SECRET"):
        raw = f"{key}=supersecret_value_xyz"
        out = redact(raw)
        assert "supersecret_value_xyz" not in out, f"secret leaked for {key}"
        assert key in out, f"key prefix lost for {key}"


def test_non_secret_text_passes_through_unchanged():
    raw = "the quick brown fox jumps over the lazy dog 1234567890"
    assert redact(raw) == raw


def test_empty_input_returns_empty():
    assert redact("") == ""
    assert redact(None) is None  # type: ignore[arg-type]


def test_idempotent():
    raw = (
        "Authorization: Bearer abcdef.123\n"
        "CF_API_TOKEN=1234567890abcdef1234567890abcdef\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----\n"
    )
    once = redact(raw)
    twice = redact(once)
    assert once == twice


def test_regular_key_not_matching_pattern_not_masked():
    # Not a secret-ish name.
    raw = "REGION=us-east-1"
    assert redact(raw) == raw
