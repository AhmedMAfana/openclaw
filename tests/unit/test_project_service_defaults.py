"""T012a: new projects default to mode='container' (FR-035).

Existing rows are untouched (FR-034) — verified indirectly: this test does
not touch any DB; it asserts the in-process default only, which is what
new-row insertion reads.
"""
from openclow.models import Project
from openclow.services.project_service import DEFAULT_PROJECT_MODE


def test_new_project_defaults_to_container():
    p = Project(name="x", github_repo="org/x")
    assert p.mode == "container", (
        "FR-035: freshly instantiated Project must default to mode='container'. "
        "If this fails, check src/openclow/models/project.py `mode` default."
    )


def test_default_constant_matches_model():
    p = Project(name="x", github_repo="org/x")
    assert DEFAULT_PROJECT_MODE == p.mode, (
        "project_service.DEFAULT_PROJECT_MODE must match the Project model default. "
        "Update both or neither."
    )


def test_legacy_modes_still_accepted():
    # FR-034: explicit legacy modes must still be constructible in memory.
    # The DB-layer CHECK constraint (migration 012) enforces the closed enum;
    # this test only guards the Python layer.
    assert Project(name="x", github_repo="org/x", mode="host").mode == "host"
    assert Project(name="x", github_repo="org/x", mode="docker").mode == "docker"
