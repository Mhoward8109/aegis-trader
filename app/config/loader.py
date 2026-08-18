"""
Configuration loader.

Spec reference: §32 (Configuration) — "Do not scatter important trading
parameters throughout source code." Every risk, scanner, strategy, and
session parameter lives in YAML, not in Python.

Load order (later overrides earlier):
  1. config/default.yaml   (checked into the repo, safe defaults)
  2. config/local.yaml      (git-ignored, operator's real settings)
  3. environment variable overrides prefixed AEGIS__ (double underscore = nesting)

The loaded Config object is immutable-by-convention: nothing in the app
should mutate it at runtime. Mode changes require restarting the process
with an edited file — this is intentional friction (spec §2: "LIVE TRADING
MUST NOT become enabled merely because the code works").
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from app.common.modes import Mode, InvalidModeError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "local.yaml"


class ConfigError(Exception):
    pass


class ModeEscalationError(ConfigError):
    """Raised when something tries to set the operating mode from a source that
    is not permitted to set it (Milestone 2 audit findings 3.5.1 / 3.5.2)."""


# `mode` is the one key in the entire config tree that may NOT be set by an
# environment variable or by an ad-hoc --config overlay. Milestone 1 allowed
# both, which meant `AEGIS__mode=LIVE` or `--config /tmp/x.yaml` could select
# LIVE while completely bypassing the documented "LIVE must be set in the
# git-ignored config/local.yaml" invariant. See docs/AUDIT_MILESTONE2.md §3.5.
PROTECTED_KEYS: frozenset[str] = frozenset({"mode"})

# Only these sources may set `mode` at all. `default.yaml` may set a non-LIVE
# mode (it ships RESEARCH/SHADOW); only `local.yaml` may set LIVE, because it is
# git-ignored and cannot arrive from a clone, a CI job, or a container image.
PERMITTED_MODE_SOURCES: tuple[str, ...] = ("default.yaml", "local.yaml")
PERMITTED_LIVE_MODE_SOURCE = "local.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env_overrides(cfg: dict) -> dict:
    """AEGIS__risk__max_daily_loss_pct=1.5 -> cfg['risk']['max_daily_loss_pct']=1.5"""
    out = copy.deepcopy(cfg)
    prefix = "AEGIS__"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path = env_key[len(prefix):].lower().split("__")
        if len(path) == 1 and path[0] in PROTECTED_KEYS:
            raise ModeEscalationError(
                f"{env_key} is not an accepted override. The operating mode may "
                f"only be set in config/default.yaml (non-LIVE) or the "
                f"git-ignored config/local.yaml (LIVE). Setting it from the "
                f"environment would bypass the LIVE authorization invariant. "
                f"See docs/AUDIT_MILESTONE2.md §3.5."
            )
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        # best-effort type coercion
        val: Any = env_val
        if env_val.lower() in ("true", "false"):
            val = env_val.lower() == "true"
        else:
            try:
                val = float(env_val) if "." in env_val else int(env_val)
            except ValueError:
                pass
        node[path[-1]] = val
    return out


class Config:
    def __init__(self, raw: dict, source_files: list[str], mode_source: str | None = None):
        self._raw = raw
        self.source_files = source_files
        # Which file actually supplied the effective `mode` value. The execution
        # authorizer treats this as evidence, not decoration: a LIVE mode whose
        # provenance is anything other than config/local.yaml is refused.
        self.mode_source = mode_source
        mode_str = raw.get("mode", "RESEARCH")
        try:
            self.mode = Mode(mode_str)
        except ValueError as e:
            raise InvalidModeError(
                f"config mode={mode_str!r} is not a valid Mode. "
                f"Valid values: {[m.value for m in Mode]}"
            ) from e

    def get(self, dotted_path: str, default: Any = None) -> Any:
        node = self._raw
        for part in dotted_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted_path: str) -> Any:
        sentinel = object()
        val = self.get(dotted_path, sentinel)
        if val is sentinel:
            raise ConfigError(f"Required config key missing: {dotted_path}")
        return val

    @property
    def raw(self) -> dict:
        return copy.deepcopy(self._raw)

    @property
    def live_mode_from_permitted_source(self) -> bool:
        """True only when mode is LIVE *and* that value provably came from the
        git-ignored config/local.yaml. Milestone 1 checked only that local.yaml
        *existed*, never that it *set* LIVE (audit §3.4)."""
        if self.mode is not Mode.LIVE:
            return False
        return self.mode_source == PERMITTED_LIVE_MODE_SOURCE

    def __repr__(self) -> str:
        return f"Config(mode={self.mode.value}, sources={self.source_files})"


def load_config(extra_path: str | None = None) -> Config:
    if not DEFAULT_CONFIG_PATH.exists():
        raise ConfigError(f"Missing required default config at {DEFAULT_CONFIG_PATH}")

    with open(DEFAULT_CONFIG_PATH) as f:
        merged = yaml.safe_load(f) or {}
    sources = [str(DEFAULT_CONFIG_PATH)]
    mode_source = "default.yaml" if "mode" in merged else None

    if LOCAL_CONFIG_PATH.exists():
        with open(LOCAL_CONFIG_PATH) as f:
            local = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, local)
        sources.append(str(LOCAL_CONFIG_PATH))
        if "mode" in local:
            mode_source = "local.yaml"

    if extra_path:
        p = Path(extra_path)
        if not p.exists():
            raise ConfigError(f"Explicit config path does not exist: {extra_path}")
        with open(p) as f:
            extra = yaml.safe_load(f) or {}
        # An ad-hoc overlay may tune risk/scanner/strategy parameters. It may
        # NOT select the operating mode: allowing that let
        # `--config /tmp/anything.yaml` with `mode: LIVE` bypass the
        # local.yaml requirement entirely (audit §3.5.2).
        if "mode" in extra:
            raise ModeEscalationError(
                f"{p} sets `mode: {extra['mode']}`. An overlay passed with "
                f"--config may not set the operating mode. Set the mode in "
                f"config/default.yaml (non-LIVE) or the git-ignored "
                f"config/local.yaml (LIVE). See docs/AUDIT_MILESTONE2.md §3.5."
            )
        merged = _deep_merge(merged, extra)
        sources.append(str(p))

    merged = _apply_env_overrides(merged)
    return Config(merged, sources, mode_source=mode_source)
