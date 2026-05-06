"""argparse + dispatch for the `chorale' console script -- the user's entry point.

Where this module sits
======================

`chorale' the binary calls into `cli.main(argv)'. This module's job is
the smallest possible "thick parser, thin main" wiring:

  parser = build_parser()              # all the help text + arg shapes
  raw    = parser.parse_args(argv)     # the user's choices, parsed
  cfg    = load_config(raw.config)     # TOML config, if any
  agents = _build_agents(...)          # role-specs -> (Backend, model) tuples
  err    = check_dependencies(agents)  # all needed CLIs on PATH?
  return run(file, agents, args, log)  # hand off to the runtime

Everything substantive lives in `run.py' / `backends.py' / `splice.py'
/ etc.; this file is responsible for parsing user input and routing
errors to the right exit code.

The help text IS the user manual
================================

The two big strings at the top (`_DESCRIPTION', `_EPILOG') are what
users see when they type `chorale --help'. They include:

  - The role-spec syntax (`role', `role@backend', `role@backend:model')
    with examples.
  - A copy-pasteable invocation that mixes four backends in one
    chorale.
  - The TOML config schema, inline.
  - Dependencies (`cotype' + at least one of claude/gemini/codex/ollama).

This is deliberate: a tool is much more pleasant to use when its
primary documentation is a couple of `--help' away rather than on a
website. It also makes chorale agent-discoverable -- an LLM that runs
`chorale --help' once knows enough to invoke the tool.

Exit codes
==========

  0 -- normal termination (Ctrl-C or all threads exited).
  2 -- usage error: bad config, unknown backend, missing CLI on PATH,
       unreadable prompt file. Anything that fails BEFORE the
       runtime is reached.
  5 -- a pending conflict was already on the file at startup.
       Returned by `run.run()'.
  Other -- unhandled exceptions propagate as Python tracebacks.
"""
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


# Top-of-help description. Has the elevator pitch and the key
# differentiator (multi-backend, splice-by-construction-no-conflicts).
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


# Bottom-of-help epilog. Worked examples + config schema + deps.
# The role-spec syntax is repeated here in tabular form because the
# 30-second user wants the syntax visible without reading the prose.
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
    """Construct the argparse parser for chorale.

    The flag surface is intentionally small:

      file               positional: path to the shared file.
      ROLE_SPEC...       positional, 1+: the agents to spawn.
      --config           override the default config-file location.
      --default-backend  override config's `defaults.backend'.
      --default-model    override config's `defaults.model'.
      --interval         tune polling cadence per agent (default 1s).
      --stagger          tune startup phase between agents (default 3s).
      --prompt-file      override the built-in brainstorm prompt.

    `--model' is registered as an alias for `--default-model' so
    chorale 0.1.0 invocations (which only had `--model') still work
    in 0.2.0+. argparse's "two flag names, one dest" pattern handles
    this without extra code.

    `RawDescriptionHelpFormatter' preserves our manually-formatted
    `_DESCRIPTION' / `_EPILOG' (line breaks, indentation) instead of
    re-wrapping them.
    """
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
    # Two flag names, one `dest' -- both `--default-model' and
    # `--model' (the 0.1.0 name) write into `args.default_model'.
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
    """Resolve every role spec into an `AgentConfig'.

    Default-backend resolution (in priority order):
      1. `--default-backend' on the CLI.
      2. `defaults.backend' in the config file.
      3. The hard-coded fallback `"claude"`.

    If the resolved default isn't in the registry (e.g., a config
    file pointing at a custom backend that wasn't actually defined),
    we ValueError with the list of known backends -- typically
    user error.

    Default-model resolution:
      1. `--default-model' on the CLI.
      2. `defaults.model' in the config file.
      3. None (the chosen Backend's `default_model' will be used).

    The default-model is passed through to `resolve_backend' for each
    role; that function applies further rules per-spec (in
    particular, `--default-model' only applies when the role spec
    didn't pick a different `@backend' -- see `resolve_backend' for
    the full priority).

    Each role spec is parsed individually; one bad spec doesn't
    prevent the rest from being parsed (well, the first bad one
    raises and the loop stops; we don't try to be clever about
    accumulating multiple errors -- the user fixes one and re-runs).
    """
    registry = build_registry(cfg)

    default_backend = cli_default_backend or cfg.default_backend or "claude"
    if default_backend not in registry:
        raise ValueError(
            f"unknown default backend {default_backend!r}; "
            f"known: {sorted(registry)}"
        )
    default_model = (
        cli_default_model
        if cli_default_model is not None
        else cfg.default_model
    )

    agents: List[AgentConfig] = []
    for spec_str in role_specs:
        spec = parse_role_spec(spec_str)
        backend, model = resolve_backend(
            spec, registry, default_backend, default_model
        )
        agents.append(
            AgentConfig(role=spec.role, backend=backend, model=model)
        )
    return agents


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse argv, resolve config + agents, hand off to `run.run()'.

    Layered error handling: each phase has a "fail fast with a clean
    error message" path that returns a non-zero exit code WITHOUT
    a Python traceback. Going down the layers:

      1. argparse -- handles its own --help / --version / bad-flag
         errors. Doesn't reach this function's body.
      2. config load -- `ConfigError' on bad TOML.
      3. agent build -- `ValueError' / `ConfigError' on bad role
         specs or misconfigured backends.
      4. dependency check -- error string if a needed CLI is missing.
      5. prompt file load -- `OSError' if --prompt-file is unreadable.
      6. run -- the runtime; returns 0 on Ctrl-C, 5 on pending conflict.

    Anything we DON'T catch propagates as a traceback (which is the
    right behaviour for genuine bugs).
    """
    raw = build_parser().parse_args(argv)

    # Phase 2: config.
    try:
        cfg = load_config(raw.config)
    except ConfigError as e:
        print(f"chorale: config error: {e}", file=sys.stderr)
        return 2

    # Phase 3: agent resolution.
    try:
        agents = _build_agents(
            raw.roles, cfg, raw.default_backend, raw.default_model
        )
    except (ValueError, ConfigError) as e:
        print(f"chorale: {e}", file=sys.stderr)
        return 2

    # Phase 4: dependency check. We do this AFTER agent resolution
    # because the dependency set depends on which backends each role
    # uses -- a pure-claude run doesn't need ollama on PATH.
    err = check_dependencies(agents)
    if err:
        print(f"chorale: {err}", file=sys.stderr)
        return 2

    # Phase 5: prompt file (optional).
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

    # The logger is a closure so the runtime doesn't have to know
    # about formatting decisions. `[role]' or `[chorale]' as a
    # column-width-padded prefix; one line per event.
    def log(role: Optional[str], msg: str) -> None:
        prefix = f"[{role}]" if role else "[chorale]"
        print(f"{prefix:<28} {msg}", flush=True)

    return run(raw.file, agents, args, log)
