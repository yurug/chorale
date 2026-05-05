"""TOML config loader for chorale.

Default location: $XDG_CONFIG_HOME/chorale/config.toml, falling back to
~/.config/chorale/config.toml. Override with --config PATH.

Schema:

    [defaults]
    backend = "claude"           # name of a built-in or [backends.NAME]
    model   = "claude-sonnet-4-6"

    # Override a built-in's default model:
    [backends.gemini]
    default_model = "gemini-2.5-pro"

    # Define a fully custom backend:
    [backends.my-local]
    command       = ["my-tool", "--prompt={prompt}", "--model={model}"]
    prompt_via    = "argv"       # or "stdin"
    default_model = "v1"
    timeout       = 90.0         # seconds
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from chorale.backends import (
    Backend,
    BackendError,
    builtin_backends,
    make_custom_backend,
)


class ConfigError(Exception):
    """Malformed config file."""


@dataclass
class Config:
    default_backend: str = "claude"
    default_model: Optional[str] = None
    # Per-backend overrides parsed from [backends.NAME] sections.
    # We store the raw config dicts here and apply them when building
    # the registry, so a `Config()` is a pure data object.
    backend_overrides: Dict[str, dict] = field(default_factory=dict)


def default_config_path() -> Path:
    """Return $XDG_CONFIG_HOME/chorale/config.toml, falling back to
    ~/.config/chorale/config.toml. The path may not exist."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "chorale" / "config.toml"


def load_config(path: Optional[Path] = None) -> Config:
    """Load config from `path`; if None, use the default location.

    Returns a default `Config()` if the file doesn't exist.
    Raises `ConfigError` on malformed TOML or invalid schema.
    """
    if path is None:
        path = default_config_path()
    if not path.exists():
        return Config()

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: {e}") from e

    cfg = Config()

    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: [defaults] must be a table")
    if "backend" in defaults:
        cfg.default_backend = str(defaults["backend"])
    if "model" in defaults:
        cfg.default_model = str(defaults["model"])

    backends = data.get("backends", {})
    if not isinstance(backends, dict):
        raise ConfigError(f"{path}: [backends] must be a table")
    for name, conf in backends.items():
        if not isinstance(conf, dict):
            raise ConfigError(
                f"{path}: [backends.{name}] must be a table"
            )
        cfg.backend_overrides[name] = conf

    return cfg


def build_registry(cfg: Config) -> Dict[str, Backend]:
    """Assemble the full backend registry: built-ins, with config
    overrides applied / custom backends added.

    Raises `ConfigError` if a custom backend's definition is malformed.
    """
    registry = builtin_backends()

    for name, conf in cfg.backend_overrides.items():
        if name in registry:
            # Override a built-in's default_model / timeout.
            b = registry[name]
            if "default_model" in conf:
                b.default_model = str(conf["default_model"])
            if "timeout" in conf:
                b.timeout = float(conf["timeout"])
        else:
            # Define a custom backend.
            cmd = conf.get("command")
            if not isinstance(cmd, list) or not all(isinstance(t, str) for t in cmd):
                raise ConfigError(
                    f"[backends.{name}]: `command` must be a list of strings"
                )
            try:
                registry[name] = make_custom_backend(
                    name=name,
                    command_template=cmd,
                    prompt_via=conf.get("prompt_via", "argv"),
                    default_model=conf.get("default_model"),
                    timeout=float(conf.get("timeout", 60.0)),
                )
            except BackendError as e:
                raise ConfigError(f"[backends.{name}]: {e}") from e
    return registry
