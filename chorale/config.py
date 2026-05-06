"""TOML config loader for chorale.

Why a config file at all
========================

Most chorale invocations don't need one; the CLI defaults (claude as
the default backend, sonnet-4-6 as its default model, the four
built-in adapters) cover the common case. The config file becomes
useful when:

  - You routinely use a non-default backend / model (e.g., `gemini'
    with a specific model the CLI's evolving default isn't pinned to).
  - You want to wire up a custom backend (a local model server, a
    research CLI, anything that takes a prompt and emits a reply).
  - You're sharing chorale invocations across machines and want the
    invocation itself short while the per-machine knobs live in
    config.

Default location
================

`$XDG_CONFIG_HOME/chorale/config.toml`, falling back to
`~/.config/chorale/config.toml'. Override with `--config PATH'.
Missing file is fine -- the loader returns a default `Config()' and
the rest of chorale runs as if no config were given.

Schema
======

::

    [defaults]
    backend = "claude"           # name of a built-in or [backends.NAME]
    model   = "claude-sonnet-4-6"

    # Override a built-in's default model + timeout:
    [backends.gemini]
    default_model = "gemini-2.5-pro"
    timeout       = 90.0

    # Define a fully custom backend:
    [backends.my-local]
    command       = ["my-tool", "--prompt={prompt}", "--model={model}"]
    prompt_via    = "argv"       # or "stdin"
    default_model = "v1"
    timeout       = 60.0         # seconds

`[defaults]' has two optional keys (`backend' and `model'). Anything
else in the table is silently ignored (forward-compat for
later-added defaults).

`[backends.NAME]' has two interpretations depending on whether NAME
is a built-in or new:

  - If NAME is a built-in (`claude', `gemini', `codex', `ollama'),
    only `default_model' and `timeout' are inspected; `command' /
    `prompt_via' are ignored. The built-in's adapter logic stays.

  - If NAME is new, ALL of `command', `prompt_via', `default_model',
    `timeout' are honoured (with `command' required). The TOML
    section is converted into a custom backend via
    `make_custom_backend`.

Why TOML, not JSON or YAML?
===========================

Three reasons:

  - `tomllib' is in the stdlib since Python 3.11 (chorale's minimum).
    No third-party dep.
  - TOML's table syntax `[backends.NAME]' is a clean fit for "a
    section per backend".
  - It's the same format `pyproject.toml' uses, which most Python
    users already understand.

YAML's indentation and JSON's lack of comments would both be steps
backward for this kind of file.
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
    """Malformed config file.

    Raised by `load_config' (TOML parse errors, wrong-type sections)
    and by `build_registry' (custom backend definitions that fail
    `make_custom_backend' validation). Distinct from `BackendError'
    which is the lower-level shape error that custom-backend factory
    raises -- `build_registry' wraps `BackendError' as `ConfigError'
    so callers see one cohesive error type for "your config is wrong".
    """


@dataclass
class Config:
    """Parsed config file -- a pure data object.

    We DO NOT instantiate `Backend' objects here; we just keep the
    raw `dict' for each `[backends.NAME]' section. The actual
    Backend assembly happens in `build_registry' (which runs after
    `load_config'). Reason: `Config' is easy to round-trip through
    serialization for tests and debugging, and any deferred work
    (resolving paths, computing binary names) is in one place.
    """

    default_backend: str = "claude"
    default_model: Optional[str] = None
    backend_overrides: Dict[str, dict] = field(default_factory=dict)


def default_config_path() -> Path:
    """Return $XDG_CONFIG_HOME/chorale/config.toml or its fallback.

    Implements the standard XDG base-dir lookup:

      - If `$XDG_CONFIG_HOME' is set, use `$XDG_CONFIG_HOME/chorale/config.toml'.
      - Otherwise, fall back to `~/.config/chorale/config.toml'.

    The path may not exist -- `load_config' handles that (returns a
    default `Config()').
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "chorale" / "config.toml"


def load_config(path: Optional[Path] = None) -> Config:
    """Load config from `path'; if None, use the default location.

    Returns a default `Config()' if the file doesn't exist (the
    "no config" case is the common one and not an error).

    Raises `ConfigError' on:
      - TOML parse errors.
      - `[defaults]' or `[backends]' not being TOML tables.
      - Any `[backends.NAME]' that isn't a TOML table.

    Schema validation that happens HERE (load_config) is shape-only:
    "is `[defaults]' a table?", "is `[backends.foo]' a table?". The
    semantic validation ("is `command' a list of strings?") happens
    in `build_registry'.
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

    # `[defaults]' section: backend + model only.
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: [defaults] must be a table")
    if "backend" in defaults:
        cfg.default_backend = str(defaults["backend"])
    if "model" in defaults:
        cfg.default_model = str(defaults["model"])

    # `[backends]' section: store raw dicts; assembly is deferred to
    # `build_registry'. Each `[backends.NAME]' must itself be a table.
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
    """Assemble the full backend registry: built-ins + config overrides.

    Two cases per `[backends.NAME]' entry:

      - NAME is a built-in -> the existing Backend's `default_model'
        and `timeout' are mutated in place (only those two fields
        are configurable on a built-in).
      - NAME is new -> a custom `Backend' is constructed via
        `make_custom_backend'.

    Raises `ConfigError' if a custom backend's definition is malformed
    (missing or wrong-typed `command', invalid `prompt_via', etc.).
    The `BackendError' from `make_custom_backend' is wrapped as
    `ConfigError' so the calling layer (`cli.main') sees one error
    type.
    """
    registry = builtin_backends()

    for name, conf in cfg.backend_overrides.items():
        if name in registry:
            # Built-in override: only `default_model' and `timeout'
            # are tunable. Any other keys in this section are
            # silently ignored.
            b = registry[name]
            if "default_model" in conf:
                b.default_model = str(conf["default_model"])
            if "timeout" in conf:
                b.timeout = float(conf["timeout"])
        else:
            # Custom backend. `command' is the required field; the
            # rest have sensible defaults.
            cmd = conf.get("command")
            if not isinstance(cmd, list) or not all(
                isinstance(t, str) for t in cmd
            ):
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
                # Wrap the lower-level shape error as a config error so
                # the user sees one error class for "your config is
                # broken".
                raise ConfigError(f"[backends.{name}]: {e}") from e
    return registry
