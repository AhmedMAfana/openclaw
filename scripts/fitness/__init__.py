"""Architecture fitness functions for TAGH Dev.

A "fitness function" (Neal Ford, *Building Evolutionary Architectures*)
is an automated check that an intended architectural property holds.
Each function in this package corresponds to ONE testable property
that the project's constitution mandates. They are run by
``scripts/pipeline_fitness.py``, which aggregates results into a
report mapped to constitution principles.

Adding a new fitness function:

  1. Drop a ``check_<short_name>.py`` file in this directory.
  2. Export a ``check()`` function that returns a ``FitnessResult``.
  3. Map it to the constitution principle it enforces in the result.

The runner discovers checks by listing this package — no central
registry. Keep each check small (≤200 lines), fast (≤2 s offline),
and deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    """Findings tier. Maps to remediation urgency.

    * ``CRITICAL`` — constitution violation or shipped runtime bug.
      Block the merge / build.
    * ``HIGH`` — likely correctness gap or contract drift; remediate
      this PR if possible.
    * ``MEDIUM`` — quality / consistency issue; ticket it.
    * ``LOW`` — style / cosmetic; deferrable.
    * ``INFO`` — context only; no action required.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


@dataclass
class Finding:
    """One issue surfaced by a fitness check."""

    severity: Severity
    message: str
    location: str | None = None
    """``file:line`` citation per Constitution Principle VII."""


@dataclass
class FitnessResult:
    """Outcome of one fitness function.

    ``passed`` is an explicit field rather than derived from ``findings``
    so a check can pass with INFO-level notes attached, or fail with
    no findings (e.g. infrastructure error).
    """

    name: str
    """Short stable identifier — used as a CLI flag and a CI job name."""
    principles: list[str]
    """Constitution principle Roman numerals this function enforces."""
    description: str
    """One-sentence summary of what the function asserts."""
    passed: bool
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    """Set when the function itself crashed (vs found a problem)."""

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.HIGH)
