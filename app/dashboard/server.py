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
from app.scanner.base import ScanCriteria, Scanner
from app.scanner.mock_provider import MockProvider
from app.strategy.scoring import OpportunityScorer, ScoreInputs

app = FastAPI(title="Aegis Trader Dashboard")


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
        "circuit_breaker_tripped": False,
        "data_source": "MockProvider (offline demo data — no credentials configured)",
    }


@app.get("/api/snapshot")
def api_snapshot():
    return _build_snapshot()


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
    <tr><th>Circuit breaker</th><td>${d.circuit_breaker_tripped ? 'TRIPPED' : 'clear'}</td></tr>
  `;
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
