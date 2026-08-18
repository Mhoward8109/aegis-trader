"""
Database schema (spec §36.4, §19 Trade Journal, §12 Order State Machine).

SQLite for the personal/single-operator deployment target (file-based,
zero-ops, trivially backed up). The schema is plain SQLAlchemy Core-style
declarative models, so swapping to Postgres later is a one-line engine URL
change (see docs/ARCHITECTURE.md, "Why SQLite first").

Design principles:
  - Append-only where practical (spec §33): rows are never destructively
    edited by strategy code; corrections are new rows referencing the old one.
  - Every table that participates in a trading decision carries a
    `data_timestamp` distinct from `created_at`, so "how old was the data
    when this decision was made" is always answerable (spec §11, §33).
  - Rejected setups are stored with the exact same fidelity as executed ones
    (spec §19: "Rejected trades are important training information").
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class OrderState(str, enum.Enum):
    PROPOSED = "PROPOSED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class TradeMode(str, enum.Enum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Candidate(Base):
    """A scanner/strategy output BEFORE any trade decision — every candidate
    considered, whether it was traded, rejected, or expired unactioned."""
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    data_timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ticker: Mapped[str] = mapped_column(String, index=True)
    strategy: Mapped[str] = mapped_column(String, index=True)
    strategy_version: Mapped[str] = mapped_column(String)
    setup_json: Mapped[dict] = mapped_column(JSON)          # raw setup snapshot
    catalyst_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    market_regime_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[float] = mapped_column(Float)
    score_breakdown_json: Mapped[dict] = mapped_column(JSON)  # component -> value, never opaque (spec §8)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    targets_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reward_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    invalidation: Mapped[str | None] = mapped_column(Text, nullable=True)
    major_risks: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String, nullable=True)
    sources_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    decision: Mapped[str] = mapped_column(String, default="PENDING")  # PENDING|ACCEPTED|REJECTED|EXPIRED
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[TradeMode] = mapped_column(Enum(TradeMode))

    orders: Mapped[list["Order"]] = relationship(back_populates="candidate")


class Order(Base):
    """One row per broker-facing (or shadow) order, with full state-machine
    history kept in OrderEvent (append-only) rather than overwritten here."""
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mode: Mapped[TradeMode] = mapped_column(Enum(TradeMode))
    ticker: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)                # BUY | SELL | SHORT | COVER
    order_type: Mapped[str] = mapped_column(String)           # market|limit|stop|stop_limit|bracket|oco|trailing_stop
    qty: Mapped[float] = mapped_column(Float)
    intended_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    submitted_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    targets_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    strategy: Mapped[str] = mapped_column(String)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[OrderState] = mapped_column(Enum(OrderState), default=OrderState.PROPOSED)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    candidate: Mapped["Candidate"] = relationship(back_populates="orders")
    events: Mapped[list["OrderEvent"]] = relationship(back_populates="order")
    performance: Mapped["TradePerformance | None"] = relationship(back_populates="order", uselist=False)


class TradePerformance(Base):
    """Immutable performance facts recorded for one PAPER order.

    This is deliberately a new, additive table rather than a destructive
    redesign of ``orders``.  It preserves every original order row and makes
    the full inputs behind a measured PAPER outcome available for later
    analysis, including rejected paper attempts.
    """
    __tablename__ = "trade_performance"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.id"), nullable=True, index=True)
    mode: Mapped[TradeMode] = mapped_column(Enum(TradeMode), default=TradeMode.PAPER)
    ticker: Mapped[str] = mapped_column(String, index=True)

    strategy_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    strategy_version: Mapped[str | None] = mapped_column(String, nullable=True)
    intended_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    targets_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    actual_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_absolute: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)

    catalyst_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_regime_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    time_of_day: Mapped[str | None] = mapped_column(String, nullable=True)
    holding_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision_data_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fill_data_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_data_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    performance_data_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    order: Mapped["Order"] = relationship(back_populates="performance")


class OrderEvent(Base):
    """Append-only audit trail of every state transition an order went
    through (spec §12, §33). Never update or delete rows in this table."""
    __tablename__ = "order_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    from_state: Mapped[str | None] = mapped_column(String, nullable=True)
    to_state: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    data_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    order: Mapped["Order"] = relationship(back_populates="events")


class RiskEvent(Base):
    """Every risk-engine decision — approvals AND rejections — with the
    inputs it used, so risk behavior itself is auditable (spec §9, §33)."""
    __tablename__ = "risk_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    candidate_id: Mapped[str | None] = mapped_column(String, nullable=True)
    decision: Mapped[str] = mapped_column(String)   # APPROVED | REJECTED
    rule_triggered: Mapped[str | None] = mapped_column(String, nullable=True)
    inputs_json: Mapped[dict] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(Text)


class CircuitBreakerEvent(Base):
    """spec §10 — the non-bypassable daily kill switch. Every trip is logged
    with the exact rule and inputs that caused it."""
    __tablename__ = "circuit_breaker_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    trigger: Mapped[str] = mapped_column(String)
    details_json: Mapped[dict] = mapped_column(JSON)
    session_date: Mapped[str] = mapped_column(String, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class StrategyVersion(Base):
    """spec §22 — Momentum-v1.0, v1.1, ... with why each version changed."""
    __tablename__ = "strategy_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    strategy_name: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    params_json: Mapped[dict] = mapped_column(JSON)
    change_rationale: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String, default="OBSERVATION")
    # OBSERVATION -> HYPOTHESIS -> PROPOSED -> BACKTESTED -> OOS_TESTED -> PAPER_TESTED -> ACCEPTED/REJECTED
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DailyReview(Base):
    __tablename__ = "daily_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    session_date: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    market_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    pnl_json: Mapped[dict] = mapped_column(JSON)
    trades_taken: Mapped[int] = mapped_column(Integer, default=0)
    trades_rejected: Mapped[int] = mapped_column(Integer, default=0)
    mistakes: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_events: Mapped[int] = mapped_column(Integer, default=0)
    what_worked: Mapped[str | None] = mapped_column(Text, nullable=True)
    what_failed: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes_worth_testing: Mapped[str | None] = mapped_column(Text, nullable=True)


def make_engine(db_path: str):
    return create_engine(f"sqlite:///{db_path}", future=True)


def init_db(db_path: str):
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(db_path: str):
    engine = init_db(db_path)
    return sessionmaker(bind=engine, future=True)
