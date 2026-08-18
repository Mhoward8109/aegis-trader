"""
The four operating modes (spec §2) and the safety governor that enforces
promotion rules between them.

MODE 0 — RESEARCH: no orders of any kind, not even hypothetical ones.
MODE 1 — SHADOW: hypothetical trades generated and journaled; nothing sent anywhere.
MODE 2 — PAPER: real orders sent, but ONLY to a broker's official paper/sim endpoint.
MODE 3 — LIVE: real money. Disabled by default; see ModeGovernor below.

Hard rule enforced in code (not just docs): the process must be able to prove,
by construction, that it cannot reach a live-trading code path unless a human
took two independent, explicit actions:
  1. Set mode: LIVE in config/local.yaml (not default.yaml, which is checked
     into version control and should never contain LIVE).
  2. Pass --i-understand-this-is-live-trading on the CLI for the specific run.

If either is missing, ModeGovernor.assert_execution_allowed() raises.
There is no code path, strategy override, or config flag that lets a
strategy module itself flip the mode.
"""
from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def allows_hypothetical_trades(self) -> bool:
        return self in (Mode.SHADOW, Mode.PAPER, Mode.LIVE)

    @property
    def allows_order_submission(self) -> bool:
        return self in (Mode.PAPER, Mode.LIVE)

    @property
    def is_real_money(self) -> bool:
        return self is Mode.LIVE

    @property
    def display_banner(self) -> str:
        return {
            Mode.RESEARCH: "RESEARCH — no orders, no hypothetical trades",
            Mode.SHADOW: "SHADOW — hypothetical trades only, nothing sent anywhere",
            Mode.PAPER: "PAPER — orders sent to broker paper/sandbox endpoint only",
            Mode.LIVE: "\u26a0\ufe0f  LIVE — REAL MONEY REAL ORDERS \u26a0\ufe0f",
        }[self]


class InvalidModeError(Exception):
    pass


class LiveTradingNotAuthorizedError(Exception):
    """Raised whenever anything tries to reach a live-money code path without
    both required, explicit human authorizations present."""


class ModeGovernor:
    """
    The single choke point every order-submitting code path MUST call before
    talking to a broker. Nothing else in the codebase is allowed to decide
    whether live trading is permitted — that would violate spec §2 and §34
    (Performance Is Not Permission).
    """

    def __init__(self, config_mode: Mode, cli_live_flag_present: bool, local_config_path_exists: bool):
        self.config_mode = config_mode
        self.cli_live_flag_present = cli_live_flag_present
        self.local_config_path_exists = local_config_path_exists

    def assert_execution_allowed(self, target: Mode) -> None:
        """Call this immediately before any broker.submit_order()-class call.
        Raises LiveTradingNotAuthorizedError or InvalidModeError; never
        silently downgrades or upgrades the mode (spec §2: 'Never silently
        fall back from paper trading to live trading' — the corollary is
        never silently escalate either)."""
        if target != self.config_mode:
            raise InvalidModeError(
                f"Execution requested for mode {target.value} but configured "
                f"mode is {self.config_mode.value}. Refusing to act outside "
                f"the configured mode."
            )
        if target is Mode.LIVE:
            if not self.local_config_path_exists:
                raise LiveTradingNotAuthorizedError(
                    "mode: LIVE must be set in config/local.yaml (a file that "
                    "is git-ignored and never shipped with defaults), not in "
                    "config/default.yaml. This file was not found."
                )
            if not self.cli_live_flag_present:
                raise LiveTradingNotAuthorizedError(
                    "LIVE mode requires the process to be started with "
                    "--i-understand-this-is-live-trading. This flag was not "
                    "supplied for this run."
                )

    def can_submit_orders(self) -> bool:
        return self.config_mode.allows_order_submission

    def can_generate_hypothetical_trades(self) -> bool:
        return self.config_mode.allows_hypothetical_trades
