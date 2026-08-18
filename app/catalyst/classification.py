"""Deterministic SEC filing classification for dilution-risk research.

ARCHITECTURAL BOUNDARY: this module only classifies and summarizes filing
metadata into a research verdict.  It MUST NOT decide whether an order is
permitted, submit an order, call an LLM, or apply probabilistic gating.  The
strategy and risk layers consume this evidence and make any trading decision.

All rules are fixed form-type and keyword checks.  Therefore identical filing
inputs always produce the identical ``DilutionRisk`` result and the module is
safe to unit test without network access.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any


class DilutionRiskLevel(StrEnum):
    NONE = "NONE"
    POSSIBLE = "POSSIBLE"
    ELEVATED = "ELEVATED"
    SEVERE = "SEVERE"


@dataclasses.dataclass(frozen=True)
class DilutionEvidence:
    """One precise SEC filing and the deterministic rule it matched."""

    accession_number: str
    form: str
    filing_date: dt.datetime | None
    title: str
    source_url: str
    rationale: str


@dataclasses.dataclass(frozen=True)
class DilutionRisk:
    """Research-only verdict; it is not an authorization or order decision."""

    level: DilutionRiskLevel
    summary: str
    filings: tuple[DilutionEvidence, ...]


_LEVEL_PRIORITY = {
    DilutionRiskLevel.NONE: 0,
    DilutionRiskLevel.POSSIBLE: 1,
    DilutionRiskLevel.ELEVATED: 2,
    DilutionRiskLevel.SEVERE: 3,
}


def _value(filing: object, name: str, default: Any = "") -> Any:
    if isinstance(filing, Mapping):
        return filing.get(name, default)
    return getattr(filing, name, default)


def _text(filing: object) -> str:
    parts = (
        _value(filing, "form"),
        _value(filing, "title"),
        _value(filing, "primary_document_description"),
        _value(filing, "items"),
        _value(filing, "category"),
        _value(filing, "description"),
    )
    return " ".join(str(part) for part in parts if part).lower()


def _classify_one(filing: object) -> tuple[DilutionRiskLevel, str] | None:
    """Return the strongest matching rule; only explicit metadata is used."""
    form = str(_value(filing, "form", "")).strip().upper().removesuffix("/A")
    text = _text(filing)

    # Evidence of an active sale/distribution is stronger than a registration
    # that merely makes future issuance possible.
    if any(term in text for term in (
        "at-the-market", "at the market", " atm ", "registered direct offering",
        "public offering", "underwritten offering", "common stock offering",
        "equity line of credit",
    )):
        return DilutionRiskLevel.SEVERE, "EDGAR metadata identifies an active equity offering or ATM program"
    if "warrant inducement" in text or "warrant exercise" in text:
        return DilutionRiskLevel.SEVERE, "EDGAR metadata identifies warrant exercise or inducement activity"

    if form.startswith("S-3") or form.startswith("S-1"):
        return DilutionRiskLevel.ELEVATED, f"Form {form} is a registration statement that can enable future securities issuance"
    if "shelf" in text:
        return DilutionRiskLevel.ELEVATED, "EDGAR metadata identifies a shelf registration"
    if "warrant" in text:
        return DilutionRiskLevel.ELEVATED, "EDGAR metadata identifies warrants that may create future share issuance"
    if form == "424B5":
        return DilutionRiskLevel.ELEVATED, "Form 424B5 is a prospectus supplement commonly used for registered offerings"

    if form.startswith("424B"):
        return DilutionRiskLevel.POSSIBLE, f"Form {form} is a prospectus filing and warrants review for issuance terms"
    if "reverse split" in text:
        return DilutionRiskLevel.POSSIBLE, "EDGAR metadata identifies a reverse split, a capital-structure event"
    return None


def _evidence_for(filing: object, rationale: str) -> DilutionEvidence:
    filed = _value(filing, "filing_date", _value(filing, "timestamp", None))
    if isinstance(filed, dt.datetime) and filed.tzinfo is None:
        filed = filed.replace(tzinfo=dt.timezone.utc)
    if not isinstance(filed, dt.datetime):
        filed = None
    accession = str(_value(filing, "accession_number", "")).strip()
    return DilutionEvidence(
        accession_number=accession,
        form=str(_value(filing, "form", "")).strip(),
        filing_date=filed,
        title=str(_value(filing, "title", _value(filing, "headline", ""))).strip(),
        source_url=str(_value(filing, "source_url", "")).strip(),
        rationale=rationale,
    )


def classify_dilution_risk(filings: Iterable[object]) -> DilutionRisk:
    """Classify SEC filing evidence without making any trading decision.

    ``filings`` may be ``SecFiling`` objects, Catalyst-like objects, or simple
    mappings containing the same metadata.  Evidence is sorted by accession
    number so a set/list with identical contents yields the same verdict.
    """
    matched: list[tuple[DilutionRiskLevel, DilutionEvidence]] = []
    for filing in filings:
        verdict = _classify_one(filing)
        if verdict is None:
            continue
        level, rationale = verdict
        matched.append((level, _evidence_for(filing, rationale)))

    if not matched:
        return DilutionRisk(
            level=DilutionRiskLevel.NONE,
            summary="No supplied SEC filing matches the deterministic dilution-risk rules.",
            filings=(),
        )

    strongest = max((level for level, _ in matched), key=lambda level: _LEVEL_PRIORITY[level])
    evidence = tuple(sorted(
        (item for level, item in matched if level == strongest),
        key=lambda item: (item.accession_number, item.form, item.source_url),
    ))
    return DilutionRisk(
        level=strongest,
        summary=(
            f"{strongest.value} dilution risk based on {len(evidence)} SEC filing(s) "
            f"matching deterministic form/keyword rules. This is research evidence, not an order decision."
        ),
        filings=evidence,
    )
