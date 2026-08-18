"""Small, non-sensitive PASS/FAIL/BLOCKED probe reports."""
from __future__ import annotations

import dataclasses
from enum import Enum


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    OPTIONAL = "OPTIONAL/UNAVAILABLE"


@dataclasses.dataclass(frozen=True)
class ProbeCheck:
    name: str
    status: CheckStatus
    detail: str = ""
    required: bool = True


@dataclasses.dataclass(frozen=True)
class ProbeReport:
    title: str
    checks: tuple[ProbeCheck, ...]

    @property
    def passed(self) -> bool:
        return all(
            check.status is CheckStatus.PASS
            for check in self.checks
            if check.required
        )

    def render(self) -> str:
        rows = [self.title]
        for check in self.checks:
            detail = f" — {check.detail}" if check.detail else ""
            rows.append(f"{check.name:.<42} {check.status.value}{detail}")
        rows.append(f"OVERALL: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(rows)
