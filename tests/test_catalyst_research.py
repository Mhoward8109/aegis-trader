"""Offline unit tests for SEC filing research; every response is synthetic."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.catalyst.classification import DilutionRiskLevel, classify_dilution_risk
from app.catalyst.sec_edgar import (
    MissingSecEdgarContactEmail,
    SecEdgarFilingProvider,
    SecFiling,
)

NOW = dt.datetime(2026, 8, 18, 16, 0, tzinfo=dt.timezone.utc)
SINCE = NOW - dt.timedelta(days=3)
TICKERS = {"0": {"ticker": "ACME", "cik_str": 123456}}


def submissions(rows: list[dict[str, str]]) -> dict:
    fields = ("form", "filingDate", "accessionNumber", "primaryDocument", "primaryDocDescription", "items")
    return {"filings": {"recent": {field: [row.get(field, "") for row in rows] for field in fields}}}


def provider_with_routes(monkeypatch, routes, **kwargs):
    """Inject an httpx mock transport, so these tests can never contact SEC."""
    monkeypatch.setenv("SEC_EDGAR_CONTACT_EMAIL", "researcher@example.test")

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path
        action = routes[key]
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request)
        status, payload = action
        return httpx.Response(status, json=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://data.sec.gov")
    return SecEdgarFilingProvider(client=client, now=lambda: NOW, **kwargs)


def test_no_catalyst_invented_when_no_filings_found(monkeypatch):
    provider = provider_with_routes(
        monkeypatch,
        {
            "/files/company_tickers.json": (200, TICKERS),
            "/submissions/CIK0000123456.json": (200, submissions([])),
        },
    )

    result = provider.fetch_catalysts("ACME", SINCE)

    assert result == []
    assert result.no_verified_catalyst
    assert result.reason.startswith("no verified catalyst found")
    assert provider.last_result is result


def test_refuses_to_request_without_contact_email(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_CONTACT_EMAIL", raising=False)
    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, json=TICKERS, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://data.sec.gov")
    provider = SecEdgarFilingProvider(client=client, now=lambda: NOW)

    with pytest.raises(MissingSecEdgarContactEmail):
        provider.fetch_catalysts("ACME", SINCE)
    assert not requested


def test_recirculated_filing_not_scored_as_new(monkeypatch):
    fixture = submissions([{
        "form": "8-K", "filingDate": "2026-08-18", "accessionNumber": "0000123456-26-000001",
        "primaryDocument": "form8k.htm", "primaryDocDescription": "Current Report", "items": "2.02",
    }])
    provider = provider_with_routes(
        monkeypatch,
        {
            "/files/company_tickers.json": (200, TICKERS),
            "/submissions/CIK0000123456.json": (200, fixture),
        },
    )

    first = provider.fetch_catalysts("ACME", SINCE)
    second = provider.fetch_catalysts("ACME", SINCE)

    assert len(first) == 1
    assert first[0].is_fresh
    assert second == []
    assert second.no_verified_catalyst
    assert "previously seen" in second.reason


def test_s3_shelf_registration_flags_dilution_risk():
    filing = SecFiling(
        ticker="ACME", cik="0000123456", form="S-3", filing_date=NOW,
        accession_number="0000123456-26-000007", primary_document="s3.htm",
        title="ACME Form S-3 filed on 2026-08-18", source_url="https://www.sec.gov/Archives/edgar/data/123456/x/s3.htm",
        primary_document_description="Shelf Registration Statement",
    )

    verdict = classify_dilution_risk([filing])

    assert verdict.level is DilutionRiskLevel.ELEVATED
    assert verdict.filings[0].accession_number == filing.accession_number
    assert "registration" in verdict.filings[0].rationale.lower()


def test_classification_is_deterministic():
    s3 = SecFiling("ACME", "0000123456", "S-3", NOW, "0000123456-26-000002", "s3.htm", "S-3", "https://sec.test/s3")
    prospectus = SecFiling("ACME", "0000123456", "424B5", NOW, "0000123456-26-000001", "p.htm", "424B5", "https://sec.test/p")

    first = classify_dilution_risk([s3, prospectus])
    second = classify_dilution_risk([prospectus, s3])

    assert first == second
    assert first.level is DilutionRiskLevel.ELEVATED
    assert [e.accession_number for e in first.filings] == ["0000123456-26-000001", "0000123456-26-000002"]


def test_filing_timestamp_used_not_wall_clock(monkeypatch):
    provider = provider_with_routes(
        monkeypatch,
        {
            "/files/company_tickers.json": (200, TICKERS),
            "/submissions/CIK0000123456.json": (200, submissions([{
                "form": "10-Q", "filingDate": "2026-08-17", "accessionNumber": "0000123456-26-000003",
                "primaryDocument": "q.htm", "primaryDocDescription": "Quarterly Report", "items": "",
            }])),
        },
    )

    result = provider.fetch_catalysts("ACME", SINCE)

    assert result[0].timestamp == dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc)
    assert result[0].retrieved_at == NOW
    assert result[0].timestamp != result[0].retrieved_at
    assert result[0].ticker == "ACME"
    assert result[0].headline == "ACME Form 10-Q filed on 2026-08-17"
    assert result[0].source_url == "https://www.sec.gov/Archives/edgar/data/123456/000012345626000003/q.htm"


def test_malformed_edgar_json_handled(monkeypatch):
    provider = provider_with_routes(
        monkeypatch,
        {
            "/files/company_tickers.json": (200, TICKERS),
            "/submissions/CIK0000123456.json": (200, {"filings": {"recent": {"form": "not-an-array"}}}),
        },
    )

    result = provider.fetch_catalysts("ACME", SINCE)

    assert result == []
    assert result.no_verified_catalyst
    assert "malformed" in result.reason


def test_rate_limit_compliance_spaces_sec_requests(monkeypatch):
    sleeps: list[float] = []
    ticks = iter((0.0, 0.0, 0.11))
    provider = provider_with_routes(
        monkeypatch,
        {
            "/files/company_tickers.json": (200, TICKERS),
            "/submissions/CIK0000123456.json": (200, submissions([])),
        },
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )

    provider.fetch_catalysts("ACME", SINCE)

    assert sleeps == [pytest.approx(0.11)]
    assert sleeps[0] >= 0.1


@pytest.mark.parametrize("failure, expected", [
    ((404, {"detail": "not found"}), "HTTP 404"),
    ((429, {"detail": "rate limited"}), "HTTP 429"),
    (httpx.ReadTimeout("timed out"), "timed out"),
])
def test_http_errors_return_verified_negative_result(monkeypatch, failure, expected):
    provider = provider_with_routes(
        monkeypatch,
        {
            "/files/company_tickers.json": (200, TICKERS),
            "/submissions/CIK0000123456.json": failure,
        },
    )

    result = provider.fetch_catalysts("ACME", SINCE)

    assert result == []
    assert result.no_verified_catalyst
    assert expected.lower() in result.reason.lower()


def test_424b_and_metadata_keywords_are_recognized_as_factual_categories(monkeypatch):
    provider = provider_with_routes(
        monkeypatch,
        {
            "/files/company_tickers.json": (200, TICKERS),
            "/submissions/CIK0000123456.json": (200, submissions([
                {"form": "424B5", "filingDate": "2026-08-18", "accessionNumber": "0000123456-26-000010", "primaryDocument": "p.htm", "primaryDocDescription": "Prospectus Supplement", "items": ""},
                {"form": "8-K", "filingDate": "2026-08-18", "accessionNumber": "0000123456-26-000011", "primaryDocument": "atm.htm", "primaryDocDescription": "At-the-Market Offering", "items": ""},
                {"form": "424B3", "filingDate": "2026-08-18", "accessionNumber": "0000123456-26-000012", "primaryDocument": "b3.htm", "primaryDocDescription": "Prospectus", "items": ""},
            ])),
        },
    )

    categories = {c.category for c in provider.fetch_catalysts("ACME", SINCE)}

    assert {"prospectus_supplement", "atm_program", "prospectus"} <= categories
