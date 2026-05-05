"""argparse + dispatch for the `chorale` console script."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from chorale import __version__
from chorale.backends import (
    Backend,
    parse_role_spec,
    resolve_backend,
)
from chorale.config import (
    Config,
    ConfigError,
    build_registry,
    load_config,
)
from chorale.prompt import DEFAULT_PROMPT
from chorale.run import AgentConfig, RunArgs, check_dependencies, run


_DESCRIPTION = """\
Run N AI agents that collaborate with you on a single text file, safely.

Each agent owns its own `## agent:<role>' section. Saves go through
cotype's 3-way merge so concurrent edits never lose work; the harness
splices each agent's reply into ONLY its own section's bytes, so two
agents editing two different sections cannot conflict by construction.

Different roles can use different brains: `role@backend` (e.g.
`reviewer@gemini`) selects a built-in or config-defined backend, and
`role@backend:model` adds a per-role model override. Built-in
backends: claude (default), gemini, codex, ollama. Custom backends
are defined in ~/.config/chorale/config.toml.
"""

_EPILOG = """\
ROLE SPEC SYNTAX:

    role                       use the default backend + default model
    role@backend               use `backend' with its default model
    role@backend:model         use `backend' with a specific model

EXAMPLE:

    chorale brainstorm.md \\
        cook \\
        logistics@gemini \\
        ux-designer@codex:gpt-5 \\
        note-taker@ollama:llama3

CONFIG FILE (~/.config/chorale/config.toml or --config PATH):

    [defaults]
    backend = "claude"
    model   = "claude-sonnet-4-6"

    [backends.gemini]
    default_model = "gemini-2.5-pro"

    [backends.my-local]
    command       = ["my-tool", "--prompt={prompt}", "--model={model}"]
    prompt_via    = "argv"          # or "stdin"
    default_model = "v1"

DEPENDENCIES (must be on PATH):

    cotype  -- https://pypi.org/project/cotype/  (`pip install cotype`)
    one of: claude, gemini, codex, ollama (whichever backends you use)

More: https://github.com/yurug/chorale
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chorale",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--version", action="version", version=f"chorale {__version__}"
    )
    p.add_argument(
        "file",
        type=Path,
        help="path to the shared Markdown file (created if absent)",
    )
    p.add_argument(
        "roles",
        nargs="+",
        metavar="ROLE_SPEC",
        help="one or more role specs: NAME, NAME@BACKEND, or NAME@BACKEND:MODEL",
    )
    p.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="config file (default: ~/.config/chorale/config.toml if present)",
    )
    # `--model` is kept as a deprecated alias for `--default-model` so
    # 0.1.0 invocations keep working.
    p.add_argument(
        "--default-model", "--model",
        dest="default_model",
        default=None,
        metavar="NAME",
        help="model for the default backend when a role spec omits a backend (overrides the config's defaults.model)",
    )
    p.add_argument(
        "--default-backend",
        dest="default_backend",
        default=None,
        metavar="NAME",
        help="default backend (overrides the config's defaults.backend, default 'claude')",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SECS",
        help="seconds between polls per agent (default: 1.0)",
    )
    p.add_argument(
        "--stagger",
        type=float,
        default=3.0,
        metavar="SECS",
        help="seconds between staggered agent startups (default: 3.0)",
    )
    p.add_argument(
        "--prompt-file",
        type=Path,
        metavar="PATH",
        help="override the built-in prompt; PATH is a `str.format` template (`{role}`, `{file_content}`)",
    )
    return p


def _build_agents(
    role_specs: Sequence[str],
    cfg: Config,
    cli_default_backend: Optional[str],
    cli_default_model: Optional[str],
) -> List[AgentConfig]:
    """Resolve every role spec into an AgentConfig.

    Default backend is the first non-None of:
      --default-backend, config defaults.backend, "claude".
    Default model (for role-without-@) is the first non-None of:
      --default-model, config defaults.model.
    """
    registry = build_registry(cfg)

    default_backend = cli_default_backend or cfg.default_backend or "claude"
    if default_backend not in registry:
        raise ValueError(
            f"unknown default backend {default_backend!r}; "
            f"known: {sorted(registry)}"
        )
    default_model = cli_default_model if cli_default_model is not None else cfg.default_model

    agents: List[AgentConfig] = []
    for spec_str in role_specs:
        spec = parse_role_spec(spec_str)
        backend, model = resolve_backend(
            spec, registry, default_backend, default_model
        )
        agents.append(AgentConfig(role=spec.role, backend=backend, model=model))
    return agents


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = build_parser().parse_args(argv)

    # Load config (file may not exist; that's fine).
    try:
        cfg = load_config(raw.config)
    except ConfigError as e:
        print(f"chorale: config error: {e}", file=sys.stderr)
        return 2

    # Resolve every role spec to an agent + backend.
    try:
        agents = _build_agents(
            raw.roles, cfg, raw.default_backend, raw.default_model
        )
    except (ValueError, ConfigError) as e:
        print(f"chorale: {e}", file=sys.stderr)
        return 2

    # Tool dependency check.
    err = check_dependencies(agents)
    if err:
        print(f"chorale: {err}", file=sys.stderr)
        return 2

    if raw.prompt_file is not None:
        try:
            prompt_template = raw.prompt_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"chorale: cannot read prompt file: {e}", file=sys.stderr)
            return 2
    else:
        prompt_template = DEFAULT_PROMPT

    args = RunArgs(
        interval=float(raw.interval),
        stagger=float(raw.stagger),
        prompt_template=prompt_template,
    )

    def log(role: Optional[str], msg: str) -> None:
        prefix = f"[{role}]" if role else "[chorale]"
        print(f"{prefix:<28} {msg}", flush=True)

    return run(raw.file, agents, args, log)
