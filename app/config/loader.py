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
    def __init__(self, raw: dict, source_files: list[str]):
        self._raw = raw
        self.source_files = source_files
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

    def __repr__(self) -> str:
        return f"Config(mode={self.mode.value}, sources={self.source_files})"


def load_config(extra_path: str | None = None) -> Config:
    if not DEFAULT_CONFIG_PATH.exists():
        raise ConfigError(f"Missing required default config at {DEFAULT_CONFIG_PATH}")

    with open(DEFAULT_CONFIG_PATH) as f:
        merged = yaml.safe_load(f) or {}
    sources = [str(DEFAULT_CONFIG_PATH)]

    if LOCAL_CONFIG_PATH.exists():
        with open(LOCAL_CONFIG_PATH) as f:
            local = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, local)
        sources.append(str(LOCAL_CONFIG_PATH))

    if extra_path:
        p = Path(extra_path)
        if not p.exists():
            raise ConfigError(f"Explicit config path does not exist: {extra_path}")
        with open(p) as f:
            extra = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, extra)
        sources.append(str(p))

    merged = _apply_env_overrides(merged)
    return Config(merged, sources)
