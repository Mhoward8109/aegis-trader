"""
Short-sale eligibility verification (Milestone 2, PART 16).

THE RULE
--------
"Do not assume a stock can be shorted simply because a strategy says SHORT. If
reliable verification is unavailable: reject the short trade. Long-only PAPER
mode is preferable to pretending short availability is known."

Milestone 1 had no shortability check at all. A strategy emitting
`direction="short"` produced a SHORT order request that went straight to the
broker, so the first thing that would have discovered a hard-to-borrow name was
the broker's rejection -- after risk had already reserved the capital and the
journal had recorded a trade.

THREE-STATE, NOT TWO-STATE
--------------------------
The mistake this module is built to avoid is collapsing "we asked and the answer
was no" together with "we could not ask". Both must reject, but they are
different failures: the first is a normal skip, the second means our data path
is broken and should be visible to the operator. So `ShortabilityVerdict.status`
distinguishes:

    SHORTABLE           broker confirms tradable + shortable
    NOT_SHORTABLE       broker confirms NOT shortable (or not tradable)
    UNVERIFIABLE        we could not obtain a reliable answer -> reject anyway

`permits_short` is True only for SHORTABLE.

ETB vs HTB
----------
Alpaca exposes `easy_to_borrow` alongside `shortable` (research §2.6). A name
that is shortable but not easy-to-borrow can still be located, but borrow
availability may vanish intraday and locate fees are unpredictable. Because this
milestone targets unattended PAPER operation, the default policy
(`require_easy_to_borrow=True`) rejects HTB names: a fill whose true cost is
unknown corrupts the very performance data PART 21 exists to collect.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from enum import Enum

log = logging.getLogger("aegis.risk.shortability")


class ShortabilityStatus(str, Enum):
    SHORTABLE = "SHORTABLE"
    NOT_SHORTABLE = "NOT_SHORTABLE"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclasses.dataclass(frozen=True)
class ShortabilityVerdict:
    ticker: str
    status: ShortabilityStatus
    reason: str
    checked_at: dt.datetime
    tradable: bool | None = None
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    asset_status: str | None = None

    @property
    def permits_short(self) -> bool:
        return self.status is ShortabilityStatus.SHORTABLE

    @property
    def is_data_failure(self) -> bool:
        """True when we could not get an answer, as opposed to getting 'no'.

        Surfaced separately in the health snapshot because a run full of
        UNVERIFIABLE verdicts means the asset lookup is broken, not that the
        market is full of hard-to-borrow names.
        """
        return self.status is ShortabilityStatus.UNVERIFIABLE

    def as_record(self) -> dict:
        return {
            "ticker": self.ticker, "status": self.status.value,
            "permits_short": self.permits_short, "reason": self.reason,
            "tradable": self.tradable, "shortable": self.shortable,
            "easy_to_borrow": self.easy_to_borrow, "asset_status": self.asset_status,
            "checked_at": self.checked_at.isoformat(),
        }


class ShortabilityGate:
    """Verifies short eligibility against the broker's asset record.

    Args:
        broker: must expose `get_asset_tradability(ticker) -> dict`. An adapter
            without it yields UNVERIFIABLE for every symbol, which correctly
            makes that adapter long-only rather than silently permissive.
        require_easy_to_borrow: reject shortable-but-HTB names (default True).
        long_only: when True, every short is rejected outright without a lookup.
            This is the recommended posture for unattended PAPER runs.
    """

    def __init__(self, broker, *, require_easy_to_borrow: bool = True,
                 long_only: bool = False):
        self.broker = broker
        self.require_easy_to_borrow = require_easy_to_borrow
        self.long_only = long_only

    def verify(self, ticker: str, direction: str,
               now: dt.datetime | None = None) -> ShortabilityVerdict:
        """Return a verdict. A LONG direction is always permitted here -- this
        gate is only about borrow availability, not about whether the trade is a
        good idea."""
        now = now or dt.datetime.now(dt.timezone.utc)
        ticker = (ticker or "").upper()

        if (direction or "").lower() != "short":
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.SHORTABLE, checked_at=now,
                reason="long trade: no borrow required, shortability gate not applicable",
            )

        if self.long_only:
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.NOT_SHORTABLE, checked_at=now,
                reason=("configuration is long_only=True, so short trades are "
                        "refused without a lookup. This is the deliberate default "
                        "posture for unattended PAPER operation."),
            )

        getter = getattr(self.broker, "get_asset_tradability", None)
        if getter is None:
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.UNVERIFIABLE, checked_at=now,
                reason=(f"{type(self.broker).__name__} cannot report asset "
                        f"tradability, so short eligibility for {ticker} cannot be "
                        f"verified. Rejecting: long-only is preferable to assuming "
                        f"borrow availability."),
            )

        try:
            info = getter(ticker)
        except Exception as exc:  # noqa: BLE001
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.UNVERIFIABLE, checked_at=now,
                reason=(f"asset lookup for {ticker} failed ({type(exc).__name__}: "
                        f"{exc}), so short eligibility is unknown. Rejecting."),
            )

        if not isinstance(info, dict):
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.UNVERIFIABLE, checked_at=now,
                reason=(f"asset lookup for {ticker} returned "
                        f"{type(info).__name__}, not a record. Rejecting."),
            )

        # The adapter is expected to distinguish "looked up and got an answer"
        # from "lookup failed". A record that cannot say which is UNVERIFIABLE,
        # because a missing `shortable` key read as falsey would be indistinguishable
        # from a confirmed False.
        if not info.get("lookup_succeeded", False):
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.UNVERIFIABLE, checked_at=now,
                reason=(f"broker reported an unsuccessful asset lookup for "
                        f"{ticker}: {info.get('error') or 'no detail given'}. "
                        f"Rejecting the short."),
            )

        tradable = info.get("tradable")
        shortable = info.get("shortable")
        etb = info.get("easy_to_borrow")
        asset_status = info.get("status")

        if shortable is None or tradable is None:
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.UNVERIFIABLE, checked_at=now,
                tradable=tradable, shortable=shortable, easy_to_borrow=etb,
                asset_status=asset_status,
                reason=(f"broker asset record for {ticker} omits "
                        f"{'shortable' if shortable is None else 'tradable'}. An "
                        f"absent flag is NOT a False -- it means unknown, so this "
                        f"is rejected as unverifiable rather than as not-shortable."),
            )

        base = dict(tradable=tradable, shortable=shortable, easy_to_borrow=etb,
                    asset_status=asset_status)

        if not tradable:
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.NOT_SHORTABLE, checked_at=now,
                reason=(f"broker reports {ticker} is not tradable "
                        f"(asset status={asset_status!r})"), **base)

        if not shortable:
            return ShortabilityVerdict(
                ticker=ticker, status=ShortabilityStatus.NOT_SHORTABLE, checked_at=now,
                reason=f"broker confirms {ticker} is not shortable", **base)

        if self.require_easy_to_borrow:
            if etb is None:
                return ShortabilityVerdict(
                    ticker=ticker, status=ShortabilityStatus.UNVERIFIABLE,
                    checked_at=now,
                    reason=(f"policy requires easy-to-borrow but the broker record "
                            f"for {ticker} does not report it. Rejecting."), **base)
            if not etb:
                return ShortabilityVerdict(
                    ticker=ticker, status=ShortabilityStatus.NOT_SHORTABLE,
                    checked_at=now,
                    reason=(f"{ticker} is shortable but HARD to borrow. Policy "
                            f"require_easy_to_borrow=True rejects it: borrow "
                            f"availability can disappear intraday and locate fees "
                            f"are unpredictable, which would corrupt the recorded "
                            f"cost of the trade."), **base)

        return ShortabilityVerdict(
            ticker=ticker, status=ShortabilityStatus.SHORTABLE, checked_at=now,
            reason=(f"broker confirms {ticker} tradable and shortable"
                    + (" and easy-to-borrow" if etb else "")), **base)
