"""Unit tests for ``_detect_project_variant`` — the pure function that
inspects a cloned project's ``composer.json`` and picks the variant
string that drives ``_variant.sh`` dispatch and the per-instance
``PROJECT_VARIANT`` env var.

No DB, no docker, no IO outside ``tmp_path``. Pure-function test.
"""
from __future__ import annotations

import json
from pathlib import Path

from taghdev.worker.tasks.instance_tasks import _detect_project_variant


def _write_composer(tmp_path: Path, require: dict, require_dev: dict | None = None) -> Path:
    composer = {"name": "test/app", "require": require}
    if require_dev:
        composer["require-dev"] = require_dev
    (tmp_path / "composer.json").write_text(json.dumps(composer))
    return tmp_path


def test_no_composer_json_falls_back_to_normal(tmp_path: Path):
    assert _detect_project_variant(str(tmp_path)) == "normal"


def test_empty_require_is_normal(tmp_path: Path):
    _write_composer(tmp_path, {"php": "^8.2"})
    assert _detect_project_variant(str(tmp_path)) == "normal"


def test_gecche_laravel_multidomain_detected(tmp_path: Path):
    """ami-digital/tagh-test uses this — main first-iteration target."""
    _write_composer(tmp_path, {
        "php": "^8.2",
        "laravel/framework": "^10.10",
        "gecche/laravel-multidomain": "10.*",
    })
    assert _detect_project_variant(str(tmp_path)) == "multidomain-gecche"


def test_spatie_multitenancy_detected(tmp_path: Path):
    _write_composer(tmp_path, {
        "php": "^8.2",
        "laravel/framework": "^11.0",
        "spatie/laravel-multitenancy": "^4.0",
    })
    assert _detect_project_variant(str(tmp_path)) == "multidomain-spatie"


def test_stancl_tenancy_detected(tmp_path: Path):
    _write_composer(tmp_path, {
        "laravel/framework": "^11.0",
        "stancl/tenancy": "^3.7",
    })
    assert _detect_project_variant(str(tmp_path)) == "multidomain-stancl"


def test_variant_package_in_require_dev_also_detected(tmp_path: Path):
    """Packages in require-dev count too — some projects ship the
    tenancy package as a dev dep when the live env is single-tenant."""
    _write_composer(
        tmp_path,
        require={"laravel/framework": "^10"},
        require_dev={"gecche/laravel-multidomain": "10.*"},
    )
    assert _detect_project_variant(str(tmp_path)) == "multidomain-gecche"


def test_malformed_composer_json_falls_back_to_normal_without_raising(tmp_path: Path):
    (tmp_path / "composer.json").write_text("{ this is not valid json")
    assert _detect_project_variant(str(tmp_path)) == "normal"


def test_first_match_wins_when_two_variants_present(tmp_path: Path):
    """In practice projects don't ship two competing tenancy packages,
    but if they do, the iteration order in ``_VARIANT_PACKAGES`` decides.
    Today gecche is first; pin that ordering so a future dict edit
    doesn't silently change behaviour."""
    _write_composer(tmp_path, {
        "gecche/laravel-multidomain": "10.*",
        "spatie/laravel-multitenancy": "^4.0",
    })
    # gecche is declared first in _VARIANT_PACKAGES → wins.
    assert _detect_project_variant(str(tmp_path)) == "multidomain-gecche"


def test_normal_laravel_skeleton_returns_normal(tmp_path: Path):
    """Sanity: a fresh `composer create-project laravel/laravel` with no
    tenancy package should still detect as 'normal'."""
    _write_composer(tmp_path, {
        "php": "^8.2",
        "laravel/framework": "^11.0",
        "laravel/sanctum": "^4.0",
        "laravel/tinker": "^2.9",
    })
    assert _detect_project_variant(str(tmp_path)) == "normal"
