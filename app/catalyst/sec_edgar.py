"""SEC EDGAR filing research provider.

This adapter only reports facts present in SEC EDGAR's company-ticker and
submissions JSON responses.  A real contact email in ``SEC_EDGAR_CONTACT_EMAIL``
is mandatory before it will issue *any* HTTP request; this is deliberately not
silently replaced with a placeholder User-Agent.

The provider returns a :class:`CatalystResearchResult`, a list-compatible result
with an explicit negative outcome when no trustworthy filing catalyst is found.
That keeps ``NewsProvider`` compatibility while allowing callers that need audit
context to inspect ``no_verified_catalyst`` and ``reason``.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from app.catalyst.engine import Catalyst, NewsProvider
from app.marketdata.freshness import check_freshness

SEC_DATA_BASE = "https://data.sec.gov"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DEFAULT_FRESHNESS_WINDOW = dt.timedelta(hours=48)
# 0.11 seconds is deliberately below the SEC's 10 requests/second ceiling.
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 0.11


class SecEdgarError(RuntimeError):
    """Base exception for SEC EDGAR adapter configuration failures."""


class MissingSecEdgarContactEmail(SecEdgarError):
    """Raised before a request when SEC_EDGAR_CONTACT_EMAIL is not configured."""


@dataclasses.dataclass(frozen=True)
class SecFiling:
    """Normalized evidence from one row of EDGAR's ``filings.recent`` arrays."""

    ticker: str
    cik: str
    form: str
    filing_date: dt.datetime
    accession_number: str
    primary_document: str
    title: str
    source_url: str
    primary_document_description: str = ""
    items: str = ""

    @property
    def searchable_text(self) -> str:
        """Only EDGAR metadata, suitable for deterministic downstream rules."""
        return " ".join((self.form, self.title, self.primary_document_description, self.items))


class CatalystResearchResult(list[Catalyst]):
    """List-compatible filing research outcome, including verified absence."""

    def __init__(
        self,
        catalysts: Iterable[Catalyst] = (),
        *,
        ticker: str,
        no_verified_catalyst: bool,
        reason: str,
        source_url: str,
    ) -> None:
        super().__init__(catalysts)
        self.ticker = ticker
        self.no_verified_catalyst = no_verified_catalyst
        self.reason = reason
        self.source_url = source_url


class SecEdgarFilingProvider(NewsProvider):
    """Fetch recent, verifiable SEC filings and expose them as catalysts.

    Network traffic is restricted to the SEC's official company tickers and
    submissions JSON endpoints.  Primary filing URLs in produced catalysts point
    to the actual EDGAR archive document, rather than a search result or a
    non-resolvable identifier.
    """

    _BASE_RELEVANT_FORMS = {"8-K", "10-Q", "10-K", "S-1", "S-3"}

    def __init__(
        self,
        *,
        freshness_window: dt.timedelta = DEFAULT_FRESHNESS_WINDOW,
        timeout: float = 10.0,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        client: httpx.Client | None = None,
        now: Callable[[], dt.datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if freshness_window.total_seconds() < 0:
            raise ValueError("freshness_window must not be negative")
        if min_request_interval_seconds < 0.1:
            raise ValueError("SEC EDGAR request interval must be at least 0.1 seconds")
        self.freshness_window = freshness_window
        self.timeout = timeout
        self.min_request_interval_seconds = min_request_interval_seconds
        self._client = client
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._last_request_at: float | None = None
        self._ticker_cik_cache: dict[str, str | None] = {}
        self._seen_accessions: set[str] = set()
        self.last_result: CatalystResearchResult | None = None

    @staticmethod
    def _contact_email() -> str:
        """Get a safe, descriptive SEC contact email without inventing one."""
        email = os.environ.get("SEC_EDGAR_CONTACT_EMAIL", "").strip()
        if not email or "@" not in email or "\n" in email or "\r" in email:
            raise MissingSecEdgarContactEmail(
                "SEC_EDGAR_CONTACT_EMAIL with a valid contact email is required before SEC EDGAR requests"
            )
        return email

    def _client_for_request(self) -> httpx.Client:
        email = self._contact_email()
        user_agent = f"AegisTrader/1.0 (SEC EDGAR research; contact: {email})"
        if self._client is None:
            self._client = httpx.Client(
                base_url=SEC_DATA_BASE,
                headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                timeout=self.timeout,
            )
        else:
            # This also applies the mandatory contact-bearing header to a
            # test-injected client.  No client is ever used without it.
            self._client.headers["User-Agent"] = user_agent
        return self._client

    def _rate_limited_get(self, path: str) -> httpx.Response:
        """Issue one compliant request, spacing starts by at least 0.11 seconds."""
        client = self._client_for_request()  # validates contact before any request
        if self._last_request_at is not None:
            remaining = self.min_request_interval_seconds - (self._monotonic() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        response = client.get(path)
        self._last_request_at = self._monotonic()
        return response

    @staticmethod
    def _json_response(response: httpx.Response) -> tuple[dict[str, Any] | None, str | None]:
        if response.status_code != 200:
            return None, f"SEC EDGAR HTTP {response.status_code}"
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return None, "malformed SEC EDGAR JSON"
        if not isinstance(payload, dict):
            return None, "malformed SEC EDGAR JSON"
        return payload, None

    @staticmethod
    def _normalise_cik(value: Any) -> str | None:
        try:
            return str(int(str(value))).zfill(10)
        except (TypeError, ValueError):
            return None

    def _cik_for_ticker(self, ticker: str) -> tuple[str | None, str | None]:
        normalized = ticker.strip().upper()
        if not normalized:
            return None, "ticker is empty"
        if normalized in self._ticker_cik_cache:
            return self._ticker_cik_cache[normalized], None
        try:
            response = self._rate_limited_get("/files/company_tickers.json")
        except httpx.TimeoutException:
            return None, "SEC EDGAR request timed out"
        except httpx.HTTPError as exc:
            return None, f"SEC EDGAR request failed: {type(exc).__name__}"
        payload, error = self._json_response(response)
        if error:
            return None, error
        assert payload is not None
        for row in payload.values():
            if not isinstance(row, dict):
                continue
            candidate = str(row.get("ticker", "")).strip().upper()
            if candidate == normalized:
                cik = self._normalise_cik(row.get("cik_str", row.get("cik")))
                if cik:
                    self._ticker_cik_cache[normalized] = cik
                    return cik, None
                return None, "malformed SEC EDGAR ticker mapping"
        self._ticker_cik_cache[normalized] = None
        return None, "ticker not present in SEC EDGAR mapping"

    @classmethod
    def _is_relevant_form(cls, form: str) -> bool:
        upper = form.strip().upper()
        base = upper.removesuffix("/A")
        return (
            base in cls._BASE_RELEVANT_FORMS
            or base.startswith("424B")
            or base.startswith("S-1")
            or base.startswith("S-3")
        )

    @staticmethod
    def _category_for(form: str, metadata: str) -> str:
        """Conservative deterministic category based solely on EDGAR metadata."""
        form_upper = form.upper().removesuffix("/A")
        words = metadata.lower()
        if re.search(r"\bat[- ]the[- ]market\b|\batm\b", words):
            return "atm_program"
        if "reverse split" in words or "reverse-split" in words:
            return "reverse_split"
        if "warrant" in words:
            return "warrant_issuance"
        if "shelf" in words:
            return "shelf_registration"
        if "prospectus supplement" in words:
            return "prospectus_supplement"
        if "offering" in words:
            return "offering"
        if form_upper.startswith("424B"):
            return "prospectus_supplement" if form_upper in {"424B5", "424B2"} else "prospectus"
        if form_upper.startswith("S-3"):
            return "shelf_registration"
        if form_upper.startswith("S-1"):
            return "registration_statement"
        if form_upper == "10-Q":
            return "quarterly_report"
        if form_upper == "10-K":
            return "annual_report"
        return "filing"

    @staticmethod
    def _significance(category: str) -> str:
        if category in {"atm_program", "offering", "warrant_issuance", "reverse_split"}:
            return "high"
        if category in {"shelf_registration", "registration_statement", "prospectus_supplement", "prospectus", "filing"}:
            return "medium"
        return "low"

    @staticmethod
    def _filing_url(cik: str, accession_number: str, primary_document: str) -> str:
        directory = f"{SEC_ARCHIVES_BASE}/{int(cik)}/{accession_number.replace('-', '')}"
        return f"{directory}/{primary_document}" if primary_document else f"{directory}/"

    @staticmethod
    def _as_text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    def _recent_filings(self, ticker: str, cik: str, payload: dict[str, Any]) -> tuple[list[SecFiling], str | None]:
        filings = payload.get("filings")
        recent = filings.get("recent") if isinstance(filings, dict) else None
        if not isinstance(recent, dict):
            return [], "malformed SEC EDGAR submissions JSON"
        required = ("form", "filingDate", "accessionNumber", "primaryDocument")
        if any(not isinstance(recent.get(field), list) for field in required):
            return [], "malformed SEC EDGAR submissions JSON"
        forms = recent["form"]
        dates = recent["filingDate"]
        accessions = recent["accessionNumber"]
        documents = recent["primaryDocument"]
        descriptions = recent.get("primaryDocDescription", [])
        items = recent.get("items", [])
        if not isinstance(descriptions, list):
            descriptions = []
        if not isinstance(items, list):
            items = []

        found: list[SecFiling] = []
        for index, (raw_form, raw_date, raw_accession, raw_document) in enumerate(zip(forms, dates, accessions, documents)):
            form = self._as_text(raw_form)
            accession = self._as_text(raw_accession)
            date_text = self._as_text(raw_date)
            document = self._as_text(raw_document)
            if not form or not accession or not self._is_relevant_form(form):
                continue
            try:
                filed = dt.datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                # A malformed row cannot be evidence. Continue without inventing
                # a timestamp or a filing URL.
                continue
            doc_description = self._as_text(descriptions[index]) if index < len(descriptions) else ""
            filing_items = self._as_text(items[index]) if index < len(items) else ""
            title = f"{ticker.upper()} Form {form} filed on {date_text}"
            found.append(SecFiling(
                ticker=ticker.upper(), cik=cik, form=form, filing_date=filed,
                accession_number=accession, primary_document=document, title=title,
                source_url=self._filing_url(cik, accession, document),
                primary_document_description=doc_description, items=filing_items,
            ))
        return found, None

    @staticmethod
    def _as_utc(value: dt.datetime) -> dt.datetime:
        return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)

    def _negative_result(self, ticker: str, reason: str, source_url: str) -> CatalystResearchResult:
        result = CatalystResearchResult(
            ticker=ticker.upper(), no_verified_catalyst=True,
            reason=f"no verified catalyst found: {reason}", source_url=source_url,
        )
        self.last_result = result
        return result

    def research(self, ticker: str, since: dt.datetime) -> CatalystResearchResult:
        """Return filing catalysts or a first-class verified-negative outcome."""
        normalized = ticker.strip().upper()
        since_utc = self._as_utc(since)
        cik, cik_error = self._cik_for_ticker(normalized)
        if not cik:
            return self._negative_result(normalized, cik_error or "CIK could not be resolved", SEC_TICKERS_URL)
        submission_path = f"/submissions/CIK{cik}.json"
        submission_url = f"{SEC_DATA_BASE}{submission_path}"
        try:
            response = self._rate_limited_get(submission_path)
        except httpx.TimeoutException:
            return self._negative_result(normalized, "SEC EDGAR request timed out", submission_url)
        except httpx.HTTPError as exc:
            return self._negative_result(normalized, f"SEC EDGAR request failed: {type(exc).__name__}", submission_url)
        payload, error = self._json_response(response)
        if error:
            return self._negative_result(normalized, error, submission_url)
        assert payload is not None
        filings, parse_error = self._recent_filings(normalized, cik, payload)
        if parse_error:
            return self._negative_result(normalized, parse_error, submission_url)

        retrieved_at = self._as_utc(self._now())
        catalysts: list[Catalyst] = []
        eligible = 0
        previously_seen = 0
        for filing in filings:
            # Mark known accessions even when they fall outside this particular
            # lookback, so a later broad query cannot portray an old filing as new.
            if filing.accession_number in self._seen_accessions:
                previously_seen += 1
                continue
            self._seen_accessions.add(filing.accession_number)
            if filing.filing_date < since_utc:
                continue
            eligible += 1
            metadata = " ".join((filing.primary_document_description, filing.items))
            category = self._category_for(filing.form, metadata)
            summary = f"SEC EDGAR records that {filing.ticker} filed Form {filing.form} on {filing.filing_date.date().isoformat()}."
            if filing.primary_document_description:
                summary += f" EDGAR describes the primary document as: {filing.primary_document_description[:180]}."
            fresh = check_freshness(
                "SEC EDGAR filing", filing.filing_date, self.freshness_window.total_seconds(), now=retrieved_at,
            ).fresh
            catalysts.append(Catalyst(
                description=summary,
                timestamp=filing.filing_date,  # publication timestamp is always EDGAR filingDate
                source="SEC EDGAR",
                source_url=filing.source_url,
                confidence="high",
                is_fresh=fresh,
                expected_significance=self._significance(category),
                category=category,
                ticker=filing.ticker,
                headline=filing.title,
                retrieved_at=retrieved_at,
            ))
        if catalysts:
            result = CatalystResearchResult(
                catalysts, ticker=normalized, no_verified_catalyst=False,
                reason="verified SEC EDGAR filings found", source_url=submission_url,
            )
            self.last_result = result
            return result
        if previously_seen:
            reason = "all relevant filings in the lookback were previously seen and are not new"
        elif eligible == 0:
            reason = "no relevant SEC EDGAR filings in the requested lookback"
        else:  # defensive: should be unreachable, but a negative remains safer than invention
            reason = "no trustworthy SEC EDGAR filing could be converted to a catalyst"
        return self._negative_result(normalized, reason, submission_url)

    def fetch_catalysts(self, ticker: str, since: dt.datetime) -> CatalystResearchResult:
        """``NewsProvider`` implementation; see :meth:`research` for metadata."""
        return self.research(ticker, since)
