"""
Builds a real `AccountState` from the broker and the journal.

WHY THIS MODULE EXISTS
----------------------
Milestone 1's pipeline constructed AccountState like this:

    account = AccountState(
        equity=snapshot.equity, buying_power=snapshot.buying_power,
        open_positions=len(open_positions),
        open_position_symbols={p.ticker for p in open_positions},
        sector_exposure_pct={}, trades_today=0,
        realized_pnl_today=0.0, realized_pnl_week=0.0,
        consecutive_losses=consecutive_losses,
    )

Four of those were hard-coded literals. The effect was not that four limits were
lenient -- it was that they could never fire at all:

  - `trades_today=0` -> max-trades-per-day compared 0 against the limit, forever.
  - `realized_pnl_today=0.0` -> the daily-loss limit saw a flat day during a
    drawdown.
  - `realized_pnl_week=0.0` -> same for the weekly limit.
  - `sector_exposure_pct={}` -> sector concentration was unmeasured.

The risk engine's tests passed the whole time, because they called
`RiskEngine.evaluate()` directly with hand-built AccountState objects that DID
contain real numbers. The limits were correct; the wiring starved them. That is
the most instructive defect in the Milestone 1 audit: a green test suite proving
a component works, sitting above a caller that never gives it the data.

The hardened `RiskEngine` now rejects when a required field is None. This builder
returns None on any aggregate it cannot compute, which converts a data-access
failure into a refused trade instead of an unmeasured limit.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from app.common.db import Order, OrderState, TradeMode
from app.risk.engine import AccountState

log = logging.getLogger("aegis.risk.account_state")

#: Order states that represent a completed, P&L-bearing trade.
_CLOSED_STATES = (OrderState.CLOSED,)

#: Order states that count as "a trade was placed today" for the trades-per-day
#: limit. Deliberately includes REJECTED and CANCELLED: a rejected submission
#: still consumed a decision and a broker interaction, and a system that retried
#: rejected orders all day without counting them would evade the limit entirely.
_ATTEMPTED_STATES = (
    OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED,
    OrderState.FILLED, OrderState.EXIT_PENDING, OrderState.CLOSED,
    OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED,
    OrderState.UNKNOWN,
)


@dataclasses.dataclass(frozen=True)
class AccountStateBuild:
    """The built state plus an explicit record of what could not be computed."""

    state: AccountState
    unavailable_fields: tuple[str, ...]
    detail: str

    @property
    def complete(self) -> bool:
        return not self.unavailable_fields

    def as_record(self) -> dict:
        return {
            "complete": self.complete,
            "unavailable_fields": list(self.unavailable_fields),
            "detail": self.detail,
            "equity": self.state.equity,
            "buying_power": self.state.buying_power,
            "open_positions": self.state.open_positions,
            "trades_today": self.state.trades_today,
            "realized_pnl_today": self.state.realized_pnl_today,
            "realized_pnl_week": self.state.realized_pnl_week,
            "consecutive_losses": self.state.consecutive_losses,
            "sector_exposure_pct": self.state.sector_exposure_pct,
        }


def build_account_state(*, broker, journal, mode: TradeMode,
                        sector_lookup: dict[str, str] | None = None,
                        now: dt.datetime | None = None) -> AccountStateBuild:
    """Assemble a real AccountState. Every field is measured or reported absent.

    Args:
        broker: queried for equity, buying power, and open positions.
        journal: queried for today's/this week's realized P&L, trade count, and
            consecutive losses.
        mode: aggregates are scoped to this mode, so a PAPER run's daily-loss
            limit is not consumed by SHADOW rehearsal losses.
        sector_lookup: {ticker: sector}. When a held ticker is absent from it,
            its exposure is attributed to "Unknown" rather than dropped --
            dropping it would understate concentration.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    sector_lookup = sector_lookup or {}
    unavailable: list[str] = []
    notes: list[str] = []

    equity = buying_power = None
    open_positions_count = None
    open_symbols = None
    sector_exposure = None

    try:
        snapshot = broker.get_account()
        equity = float(snapshot.equity)
        buying_power = float(snapshot.buying_power)
    except Exception as exc:  # noqa: BLE001
        unavailable.extend(["equity", "buying_power"])
        notes.append(f"broker account snapshot failed: {exc}")

    try:
        positions = broker.get_positions()
        open_positions_count = len(positions)
        open_symbols = {p.ticker for p in positions}
        if equity and equity > 0:
            exposure: dict[str, float] = {}
            for p in positions:
                sector = sector_lookup.get(p.ticker, "Unknown")
                notional = abs(float(p.qty) * float(p.current_price))
                exposure[sector] = exposure.get(sector, 0.0) + (notional / equity) * 100.0
            sector_exposure = exposure
        else:
            unavailable.append("sector_exposure_pct")
            notes.append("sector exposure needs equity, which is unavailable, so "
                         "exposure percentages cannot be computed")
    except Exception as exc:  # noqa: BLE001
        unavailable.extend(["open_positions", "open_position_symbols",
                            "sector_exposure_pct"])
        notes.append(f"broker position query failed: {exc}")

    try:
        trades_today = _count_trades_today(journal, mode=mode, now=now)
    except Exception as exc:  # noqa: BLE001
        trades_today = None
        unavailable.append("trades_today")
        notes.append(f"trades-today count failed: {exc}")

    try:
        realized_today = _realized_pnl_since(
            journal, mode=mode, since=_start_of_day(now), now=now)
    except Exception as exc:  # noqa: BLE001
        realized_today = None
        unavailable.append("realized_pnl_today")
        notes.append(f"today's realized P&L failed: {exc}")

    try:
        realized_week = _realized_pnl_since(
            journal, mode=mode, since=_start_of_week(now), now=now)
    except Exception as exc:  # noqa: BLE001
        realized_week = None
        unavailable.append("realized_pnl_week")
        notes.append(f"this week's realized P&L failed: {exc}")

    try:
        consecutive_losses = _consecutive_losses(journal, mode=mode)
    except Exception as exc:  # noqa: BLE001
        consecutive_losses = None
        unavailable.append("consecutive_losses")
        notes.append(f"consecutive-loss count failed: {exc}")

    state = AccountState(
        equity=equity, buying_power=buying_power,
        open_positions=open_positions_count,
        open_position_symbols=open_symbols,
        sector_exposure_pct=sector_exposure,
        trades_today=trades_today,
        realized_pnl_today=realized_today,
        realized_pnl_week=realized_week,
        consecutive_losses=consecutive_losses,
    )

    if unavailable:
        detail = (f"AccountState is INCOMPLETE. Unavailable: "
                  f"{', '.join(sorted(set(unavailable)))}. "
                  + " ".join(notes)
                  + " Every risk limit depending on a missing field will reject "
                    "rather than treat it as zero.")
        log.error(detail)
    else:
        detail = (f"AccountState complete: equity={equity:.2f} "
                  f"buying_power={buying_power:.2f} positions={open_positions_count} "
                  f"trades_today={trades_today} pnl_today={realized_today:.2f} "
                  f"pnl_week={realized_week:.2f} consec_losses={consecutive_losses}")

    return AccountStateBuild(state=state,
                             unavailable_fields=tuple(sorted(set(unavailable))),
                             detail=detail)


# -- journal aggregates -----------------------------------------------------
def _start_of_day(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_week(now: dt.datetime) -> dt.datetime:
    """Monday 00:00 UTC of the current week.

    Uses a calendar week rather than a trailing 7 days, because a trailing
    window would let a large Friday loss keep suppressing trading into the
    following week -- and, worse, would silently reset mid-week.
    """
    monday = now - dt.timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _count_trades_today(journal, *, mode: TradeMode, now: dt.datetime) -> int:
    start = _start_of_day(now)
    return (journal.session.query(Order)
            .filter(Order.mode == mode,
                    Order.created_at >= start,
                    Order.state.in_(_ATTEMPTED_STATES))
            .count())


def _realized_pnl_since(journal, *, mode: TradeMode, since: dt.datetime,
                        now: dt.datetime) -> float:
    """Sum realized P&L on closed orders in the window.

    Orders in a closed state whose `realized_pnl` is NULL are a data gap, not a
    zero. Rather than quietly summing them as 0.0 -- which would understate a
    drawdown and could keep the daily-loss limit from firing -- this raises, and
    the caller converts that into a missing field, which makes the limit reject.
    """
    rows = (journal.session.query(Order)
            .filter(Order.mode == mode,
                    Order.closed_at.isnot(None),
                    Order.closed_at >= since,
                    Order.state.in_(_CLOSED_STATES))
            .all())
    missing = [o.id for o in rows if o.realized_pnl is None]
    if missing:
        raise ValueError(
            f"{len(missing)} closed order(s) since {since.isoformat()} have NULL "
            f"realized_pnl (e.g. {missing[:3]}). Treating them as 0.0 would "
            f"understate the drawdown and could stop the daily-loss limit from "
            f"firing, so this is reported as unavailable instead."
        )
    return float(sum(o.realized_pnl for o in rows))


def _consecutive_losses(journal, *, mode: TradeMode) -> int:
    """Count losing closed trades back from the most recent one.

    Read-only, and used only to VETO. There is deliberately no path from this
    number to a larger position size -- see `RiskEngine.compute_position_size`,
    which does not accept it as an argument at all.
    """
    rows = (journal.session.query(Order)
            .filter(Order.mode == mode,
                    Order.closed_at.isnot(None),
                    Order.state.in_(_CLOSED_STATES))
            .order_by(Order.closed_at.desc())
            .limit(50).all())
    streak = 0
    for o in rows:
        if o.realized_pnl is None:
            raise ValueError(
                f"closed order {o.id} has NULL realized_pnl, so the consecutive-loss "
                f"streak cannot be determined. Reporting unavailable rather than "
                f"assuming it was a winner.")
        if o.realized_pnl < 0:
            streak += 1
        else:
            break
    return streak
