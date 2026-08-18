"""Pure, deterministic calculations for recorded trade-performance facts."""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime


def calculate_slippage(intended_entry: float, actual_fill_price: float) -> tuple[float, float]:
    """Return absolute price slippage and absolute percent slippage.

    The result deliberately describes execution variance, not whether that
    variance was favorable or adverse.  Both values are therefore non-negative
    and can be compared uniformly across long and short records.
    """
    _require_finite_positive("intended_entry", intended_entry)
    _require_finite_positive("actual_fill_price", actual_fill_price)
    absolute = abs(actual_fill_price - intended_entry)
    return absolute, absolute / intended_entry * 100.0


def calculate_r_multiple(
    *,
    entry_price: float,
    stop_price: float,
    exit_price: float | None = None,
    direction: str,
    quantity: float = 1.0,
    realized_pnl: float | None = None,
) -> float:
    """Compute realized R from recorded prices/quantity or recorded P&L.

    R is ``realized_pnl / initial_dollar_risk``.  If broker-recorded
    ``realized_pnl`` is supplied, it is authoritative; otherwise the realized
    P&L is derived from entry/exit prices and direction.
    """
    _require_finite_positive("entry_price", entry_price)
    _require_finite_positive("stop_price", stop_price)
    _require_finite_positive("quantity", quantity)
    direction = _normalise_direction(direction)
    initial_risk = abs(entry_price - stop_price) * quantity
    if initial_risk == 0:
        raise ValueError("R multiple is undefined when entry_price equals stop_price.")

    if realized_pnl is None:
        if exit_price is None:
            raise ValueError("exit_price is required when realized_pnl is not supplied.")
        _require_finite_positive("exit_price", exit_price)
        multiplier = 1.0 if direction == "long" else -1.0
        realized_pnl = (exit_price - entry_price) * quantity * multiplier
    elif not _is_finite(realized_pnl):
        raise ValueError(f"realized_pnl must be finite; received {realized_pnl!r}.")
    return realized_pnl / initial_risk


def calculate_mfe_mae(
    *,
    entry_price: float,
    highs: Iterable[float],
    lows: Iterable[float],
    direction: str,
) -> tuple[float, float]:
    """Return max favorable and adverse excursion in absolute price units.

    ``highs`` and ``lows`` are the observed high/low prices while the trade was
    open.  MFE and MAE are non-negative: a trade that never moved in one
    direction has a zero excursion for that direction.
    """
    _require_finite_positive("entry_price", entry_price)
    direction = _normalise_direction(direction)
    high_values = list(highs)
    low_values = list(lows)
    if not high_values or not low_values:
        raise ValueError("highs and lows must both contain at least one recorded price.")
    if any(not _is_finite_positive(value) for value in high_values + low_values):
        raise ValueError("highs and lows must contain only finite positive prices.")

    if direction == "long":
        return max(0.0, max(high_values) - entry_price), max(0.0, entry_price - min(low_values))
    return max(0.0, entry_price - min(low_values)), max(0.0, max(high_values) - entry_price)


def calculate_holding_duration(entry_timestamp: datetime, exit_timestamp: datetime) -> float:
    """Return holding duration in seconds from recorded timestamps."""
    if not isinstance(entry_timestamp, datetime) or not isinstance(exit_timestamp, datetime):
        raise ValueError("entry_timestamp and exit_timestamp must both be datetime values.")
    duration = (exit_timestamp - entry_timestamp).total_seconds()
    if duration < 0:
        raise ValueError("exit_timestamp cannot be earlier than entry_timestamp.")
    return duration


def _normalise_direction(direction: str) -> str:
    if not isinstance(direction, str):
        raise ValueError(f"direction must be 'long' or 'short'; received {direction!r}.")
    if direction.lower() in {"long", "buy"}:
        return "long"
    if direction.lower() in {"short", "sell"}:
        return "short"
    raise ValueError(f"direction must be 'long' or 'short'; received {direction!r}.")


def _is_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_finite_positive(value: object) -> bool:
    return _is_finite(value) and value > 0


def _require_finite_positive(name: str, value: object) -> None:
    if not _is_finite_positive(value):
        raise ValueError(f"{name} must be finite and > 0; received {value!r}.")
