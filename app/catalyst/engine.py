"""
Catalyst Engine (spec §5). Determines WHY a security is moving, from
verifiable sources only — "Never invent a catalyst" is the hard rule.

IMPORTANT ARCHITECTURAL NOTE (see docs/ARCHITECTURE.md "Catalyst data
sourcing"): this module runs as a standalone deployed process, NOT inside
an agent tool-calling session, so it cannot use Perplexity's internal
search tools. It is built around pluggable NewsProvider adapters that call
real public/commercial APIs directly. Two are provided out of the box:

  - SecEdgarProvider: SEC EDGAR full-text search + submissions API. Free,
    no API key, requires only a compliant User-Agent header (SEC policy).
  - NullNewsProvider: always returns "no data available", used as a safe
    default so the engine degrades gracefully instead of crashing or (worse)
    inventing news, when no paid news API key is configured (spec §4: "Do
    not assume every data provider supplies every field. Gracefully
    identify unavailable information.").

To add a real market-news feed (recommended: Benzinga News API or
MT Newswires — see docs/ARCHITECTURE.md for why), implement NewsProvider
and register it in app/config — no other module needs to change.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt
import time

import httpx

SEC_EDGAR_BASE = "https://data.sec.gov"
SEC_USER_AGENT = "AegisTrader/1.0 (personal research tool; contact: set SEC_EDGAR_CONTACT_EMAIL env var)"


@dataclasses.dataclass
class Catalyst:
    description: str
    timestamp: dt.datetime
    source: str
    source_url: str
    confidence: str            # low | medium | high
    is_fresh: bool
    expected_significance: str  # low | medium | high
    category: str               # earnings|filing|fda|ma|partnership|analyst|legal|management|offering|... 


class NewsProvider(abc.ABC):
    @abc.abstractmethod
    def fetch_catalysts(self, ticker: str, since: dt.datetime) -> list[Catalyst]: ...


class NullNewsProvider(NewsProvider):
    """Safe default. Never fabricates a catalyst; explicitly reports absence."""

    def fetch_catalysts(self, ticker: str, since: dt.datetime) -> list[Catalyst]:
        return []


class SecEdgarProvider(NewsProvider):
    """Free, keyless, but rate-limited and requires a descriptive User-Agent
    per SEC's fair-access policy. Surfaces 8-K/S-1/424B/SC 13D-G/Form 4/S-3
    filings as catalyst candidates — genuinely verifiable, never invented."""

    RELEVANT_FORMS = {"8-K", "S-1", "424B5", "424B3", "SC 13D", "SC 13G", "4", "S-3", "6-K"}

    def __init__(self, contact_email: str | None = None, timeout: float = 10.0):
        ua = SEC_USER_AGENT
        if contact_email:
            ua = f"AegisTrader/1.0 (personal research tool; contact: {contact_email})"
        self._client = httpx.Client(base_url=SEC_EDGAR_BASE, headers={"User-Agent": ua}, timeout=timeout)
        self._last_call = 0.0

    def _rate_limited_get(self, path: str, **kwargs) -> httpx.Response:
        # SEC EDGAR asks for <=10 requests/second; we stay well under that.
        elapsed = time.monotonic() - self._last_call
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        resp = self._client.get(path, **kwargs)
        self._last_call = time.monotonic()
        return resp

    def _cik_for_ticker(self, ticker: str) -> str | None:
        resp = self._rate_limited_get("/files/company_tickers.json")
        if resp.status_code != 200:
            return None
        data = resp.json()
        for _, row in data.items() if isinstance(data, dict) else enumerate(data):
            entry = row if isinstance(row, dict) else row
            if isinstance(entry, dict) and entry.get("ticker", "").upper() == ticker.upper():
                return str(entry["cik_str"]).zfill(10)
        return None

    def fetch_catalysts(self, ticker: str, since: dt.datetime) -> list[Catalyst]:
        cik = self._cik_for_ticker(ticker)
        if not cik:
            return []
        resp = self._rate_limited_get(f"/submissions/CIK{cik}.json")
        if resp.status_code != 200:
            return []
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        out: list[Catalyst] = []
        for form, date_str, accn, doc in zip(forms, dates, accessions, primary_docs):
            if form not in self.RELEVANT_FORMS:
                continue
            filed = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            if filed < since:
                continue
            accn_nodash = accn.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_nodash}/{doc}"
            out.append(Catalyst(
                description=f"{ticker} filed Form {form} with the SEC on {date_str}.",
                timestamp=filed, source="SEC EDGAR", source_url=url,
                confidence="high",  # primary source
                is_fresh=(dt.datetime.now(dt.timezone.utc) - filed) < dt.timedelta(hours=48),
                expected_significance="medium" if form == "8-K" else "low",
                category="filing",
            ))
        return out


class CatalystEngine:
    def __init__(self, providers: list[NewsProvider]):
        self.providers = providers

    def research(self, ticker: str, lookback_hours: int = 72) -> list[Catalyst]:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
        catalysts: list[Catalyst] = []
        for p in self.providers:
            try:
                catalysts.extend(p.fetch_catalysts(ticker, since))
            except Exception as e:  # a provider failing must not crash the scan
                catalysts.append(Catalyst(
                    description=f"Catalyst provider {type(p).__name__} failed: {e}",
                    timestamp=dt.datetime.now(dt.timezone.utc), source=type(p).__name__,
                    source_url="", confidence="low", is_fresh=False,
                    expected_significance="low", category="provider_error",
                ))
        catalysts.sort(key=lambda c: c.timestamp, reverse=True)
        return catalysts

    def quality_score(self, catalysts: list[Catalyst]) -> float:
        """0-1 input for OpportunityScorer.catalyst_quality. No catalysts ->
        0 (not penalized elsewhere; a 'NO TRADE' output is fine, spec §35)."""
        if not catalysts:
            return 0.0
        weight = {"high": 1.0, "medium": 0.6, "low": 0.3}
        real = [c for c in catalysts if c.category != "provider_error"]
        if not real:
            return 0.0
        return max(weight.get(c.expected_significance, 0.3) * (1.0 if c.confidence == "high" else 0.7) for c in real)

    def freshness_score(self, catalysts: list[Catalyst]) -> float:
        real = [c for c in catalysts if c.category != "provider_error"]
        if not real:
            return 0.0
        return 1.0 if any(c.is_fresh for c in real) else 0.2
