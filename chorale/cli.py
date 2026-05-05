"""argparse + dispatch for the `chorale` console script."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from chorale import __version__
from chorale.prompt import DEFAULT_PROMPT
from chorale.run import _Args, check_dependencies, run


_DESCRIPTION = """\
Run N AI agents that collaborate with you on a single text file, safely.

Each agent owns its own `## agent:<role>' section. Saves go through
cotype's 3-way merge so concurrent edits never lose work; the harness
splices each agent's reply into ONLY its own section's bytes, so two
agents editing two different sections cannot conflict by construction.

You watch the file in any editor (preferably one with cotype-mode), the
agents see your edits on their next poll, and the conversation
accumulates in the file rather than scrolling away in a chat window.
"""

_EPILOG = """\
DEPENDENCIES (must be on PATH):
    cotype  -- https://pypi.org/project/cotype/  (`pip install cotype`)
    claude  -- the Claude Code CLI

EXAMPLE:
    chorale brainstorm.md cook logistics ux-designer note-taker

While running, edit `brainstorm.md' in any editor; agents see your
saves on their next poll and respond. Stop with Ctrl-C.

CUSTOM PROMPTS:
    Pass --prompt-file PATH to override the built-in brainstorm prompt.
    The file is treated as a `str.format` template with placeholders
    `{role}` and `{file_content}`.

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
        metavar="ROLE",
        help="one or more agent role names (e.g. cook logistics ux-designer)",
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
        "--model",
        default="claude-sonnet-4-6",
        metavar="NAME",
        help="Claude model name (default: claude-sonnet-4-6; pass empty for CLI default)",
    )
    p.add_argument(
        "--prompt-file",
        type=Path,
        metavar="PATH",
        help="override the built-in prompt; PATH is a `str.format` template (`{role}`, `{file_content}`)",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = build_parser().parse_args(argv)

    err = check_dependencies()
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

    args = _Args()
    args.interval = float(raw.interval)
    args.stagger = float(raw.stagger)
    args.model = raw.model or None
    args.prompt_template = prompt_template

    def log(role: Optional[str], msg: str) -> None:
        prefix = f"[{role}]" if role else "[chorale]"
        print(f"{prefix:<28} {msg}", flush=True)

    return run(raw.file, list(raw.roles), args, log)
