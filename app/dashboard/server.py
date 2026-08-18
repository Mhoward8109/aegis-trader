"""
Dashboard (spec §25). FastAPI backend serving a single-page operator view.
Reads live state from the scanner/catalyst/scoring pipeline (MockProvider by
default so this runs with zero credentials) — swap MockProvider for a real
adapter in app/scanner/ once market-data credentials are configured.

The mode banner is rendered from config, not guessed — and LIVE is styled
to be visually unmistakable per spec §25/§2.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.catalyst.engine import CatalystEngine, NullNewsProvider
from app.config.loader import load_config
from app.observability.health import build_health_snapshot
from app.risk.persistent_circuit_breaker import (
    PersistentCircuitBreaker,
    default_breaker_path,
)
from app.scanner.base import ScanCriteria, Scanner
from app.scanner.mock_provider import MockProvider
from app.strategy.scoring import OpportunityScorer, ScoreInputs

app = FastAPI(title="Aegis Trader Dashboard")


def _build_health_snapshot(cfg, provider: MockProvider) -> dict:
    """Build an honest health response even while this demo dashboard is offline."""
    breaker = PersistentCircuitBreaker(
        default_breaker_path(), cfg=cfg.get("circuit_breaker", {})
    )
    strategy_specs = [
        {"name": name, "version": None}
        for name in (cfg.get("strategies.enabled", []) or [])
    ]
    return build_health_snapshot(
        mode=cfg.mode,
        circuit_breaker=breaker,
        market_data_provider=provider,
        strategies=strategy_specs,
        # This dashboard only runs an offline MockProvider and does not create a
        # broker adapter, journal, freshness report, or reconciliation pass.
        # Marking those absences is intentional: an operator must not mistake
        # the dashboard demo for evidence that live entry gates are healthy.
        broker_adapter_enabled=False,
        broker_adapter_enabled_reason=(
            "Dashboard is running with offline demo data; no broker adapter is instantiated."
        ),
    ).as_record()


def _build_snapshot():
    cfg = load_config()
    provider = MockProvider()
    criteria = ScanCriteria(
        price_min=cfg.get("scanner.price_min"), price_max=cfg.get("scanner.price_max"),
        rvol_min=cfg.get("scanner.rvol_min"), dollar_volume_min=cfg.get("scanner.dollar_volume_min"),
        max_spread_pct=cfg.get("scanner.max_spread_pct"),
    )
    scan = Scanner(provider, criteria).run()
    scorer = OpportunityScorer(cfg.get("scoring.weights"))
    catalyst_engine = CatalystEngine([NullNewsProvider()])

    opportunities = []
    for r in scan["results"]:
        catalysts = catalyst_engine.research(r.ticker)
        inputs = ScoreInputs(
            catalyst_quality=catalyst_engine.quality_score(catalysts),
            catalyst_freshness=catalyst_engine.freshness_score(catalysts),
            relative_volume=r.fields.get("rvol", 0), liquidity_usd=r.fields.get("dollar_volume", 0),
            spread_pct=r.fields.get("spread_pct", 1.0), technical_alignment=0.5,
            market_trend_alignment=0.5, reward_risk=2.0,
            historical_strategy_expectancy_r=None, data_confidence=0.8,
        )
        s = scorer.score(inputs)
        opportunities.append({"ticker": r.ticker, "score": s["score"], "breakdown": s["breakdown"], **r.fields})
    opportunities.sort(key=lambda o: -o["score"])
    health = _build_health_snapshot(cfg, provider)
    breaker = health["circuit_breaker_state"]
    breaker_value = breaker["value"] if breaker["availability"] == "AVAILABLE" else None

    return {
        "mode": cfg.mode.value,
        "mode_banner": cfg.mode.display_banner,
        "risk": {
            "max_daily_loss_pct": cfg.get("risk.max_daily_loss_pct"),
            "max_trades_per_day": cfg.get("risk.max_trades_per_day"),
            "max_concurrent_positions": cfg.get("risk.max_concurrent_positions"),
        },
        "opportunities": opportunities[:10],
        "positions": [],  # wire to a live BrokerAdapter.get_positions() once configured
        # Never replace an unread breaker with False: that manufactures false
        # confidence, which is worse for an operator than no dashboard at all.
        "circuit_breaker_tripped": (
            breaker_value["tripped"] if breaker_value is not None else None
        ),
        "health": health,
        "data_source": "MockProvider (offline demo data — no credentials configured)",
    }


@app.get("/api/snapshot")
def api_snapshot():
    return _build_snapshot()


@app.get("/health")
def health():
    """Machine-readable, fail-closed operator health snapshot."""
    cfg = load_config()
    return _build_health_snapshot(cfg, MockProvider())


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_render_html())


def _render_html() -> str:
    return """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Aegis Trader</title>
<style>
  body { background:#0b0e14; color:#e6e6e6; font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .banner { padding:10px 16px; border-radius:8px; font-weight:600; margin-bottom:20px; display:inline-block; }
  .RESEARCH { background:#1c2b3a; color:#7ec8ff; }
  .SHADOW { background:#2a1c3a; color:#c99bff; }
  .PAPER { background:#173a24; color:#7bffab; }
  .LIVE { background:#4a0d0d; color:#ff5c5c; border:2px solid #ff5c5c; animation:pulse 1.2s infinite; }
  @keyframes pulse { 0%{opacity:1;} 50%{opacity:.55;} 100%{opacity:1;} }
  table { width:100%; border-collapse:collapse; margin-top:10px; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid #222836; font-size:13px; }
  th { color:#8a94a6; font-weight:500; }
  .panel { background:#11151d; border:1px solid #1e2430; border-radius:10px; padding:16px; margin-bottom:20px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  .note { color:#8a94a6; font-size:12px; margin-top:8px; }
  .score-hi { color:#7bffab; font-weight:600; }
  .score-mid { color:#ffd27b; }
  .score-lo { color:#8a94a6; }
</style>
</head>
<body>
  <h1>Aegis Trader</h1>
  <div id="banner" class="banner">Loading...</div>
  <div class="grid">
    <div class="panel">
      <h3>Top Opportunities</h3>
      <table id="opps"><thead><tr>
        <th>Ticker</th><th>Score</th><th>Price</th><th>%Chg</th><th>RVOL</th><th>$Vol</th><th>Spread%</th>
      </tr></thead><tbody></tbody></table>
      <div class="note" id="datasource"></div>
    </div>
    <div class="panel">
      <h3>Risk Panel</h3>
      <table id="risk"><tbody></tbody></table>
    </div>
  </div>
  <div class="panel">
    <h3>Open Positions</h3>
    <table id="positions"><thead><tr><th>Ticker</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th></tr></thead>
    <tbody><tr><td colspan="5" class="note">No open positions (or no broker connected).</td></tr></tbody></table>
  </div>
  <div class="panel">
    <h3>System Health</h3>
    <table id="health"><tbody></tbody></table>
    <pre class="note" id="healthjson"></pre>
  </div>

<script>
async function refresh() {
  const r = await fetch('/api/snapshot');
  const d = await r.json();
  const banner = document.getElementById('banner');
  banner.textContent = d.mode_banner;
  banner.className = 'banner ' + d.mode;

  const oppsBody = document.querySelector('#opps tbody');
  oppsBody.innerHTML = d.opportunities.map(o => {
    const cls = o.score >= 65 ? 'score-hi' : (o.score >= 40 ? 'score-mid' : 'score-lo');
    return `<tr><td>${o.ticker}</td><td class="${cls}">${o.score}</td><td>$${o.price}</td>` +
           `<td>${o.pct_change}%</td><td>${o.rvol}x</td><td>$${Math.round(o.dollar_volume/1e6)}M</td>` +
           `<td>${o.spread_pct}%</td></tr>`;
  }).join('');
  document.getElementById('datasource').textContent = d.data_source;

  const riskBody = document.querySelector('#risk tbody');
  riskBody.innerHTML = `
    <tr><th>Max daily loss</th><td>${d.risk.max_daily_loss_pct}%</td></tr>
    <tr><th>Max trades/day</th><td>${d.risk.max_trades_per_day}</td></tr>
    <tr><th>Max concurrent positions</th><td>${d.risk.max_concurrent_positions}</td></tr>
    <tr><th>Circuit breaker</th><td>${d.circuit_breaker_tripped === true ? 'TRIPPED' : (d.circuit_breaker_tripped === false ? 'clear' : 'UNKNOWN')}</td></tr>
  `;

  const h = d.health;
  const display = field => {
    if (field.availability !== 'AVAILABLE') return `${field.availability}: ${field.reason}`;
    return typeof field.value === 'object' ? JSON.stringify(field.value) : String(field.value);
  };
  document.querySelector('#health tbody').innerHTML = `
    <tr><th>Overall status</th><td>${h.status}</td></tr>
    <tr><th>Blocking reasons</th><td>${h.blocking_reasons.length ? h.blocking_reasons.join(' | ') : 'None'}</td></tr>
    <tr><th>Mode</th><td>${display(h.mode)}</td></tr>
    <tr><th>Broker environment</th><td>${display(h.broker_environment)}</td></tr>
    <tr><th>Broker adapter enabled</th><td>${display(h.broker_adapter_enabled)}</td></tr>
    <tr><th>Market data health</th><td>${display(h.market_data_health)}</td></tr>
    <tr><th>Last quote age</th><td>${display(h.last_quote_age_seconds)}</td></tr>
    <tr><th>Reconciliation</th><td>${display(h.reconciliation_state)}</td></tr>
    <tr><th>Circuit breaker</th><td>${display(h.circuit_breaker_state)}</td></tr>
    <tr><th>Open positions</th><td>${display(h.open_positions)}</td></tr>
    <tr><th>Open orders</th><td>${display(h.open_orders)}</td></tr>
    <tr><th>Realized P&amp;L</th><td>${display(h.realized_pnl)}</td></tr>
    <tr><th>Unrealized P&amp;L</th><td>${display(h.unrealized_pnl)}</td></tr>
    <tr><th>Remaining daily risk budget</th><td>${display(h.remaining_daily_risk_budget)}</td></tr>
  `;
  document.getElementById('healthjson').textContent = JSON.stringify(h, null, 2);
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
