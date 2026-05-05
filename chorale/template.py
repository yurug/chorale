"""Pre-allocate a Markdown section template for the user + N agents.

Why pre-allocate: cotype's merge is line-based (POSIX diff3); without
unique unchanged anchors between sections, edits in adjacent sections
can fall in the same diff3 hunk and conflict spuriously. Each section
gets a unique placeholder body so diff3 always has stable per-section
anchors.

Pure function. No filesystem; the caller decides where to write.
"""
from __future__ import annotations

from typing import Iterable


def render_template(roles: Iterable[str]) -> bytes:
    """Render the initial brainstorm.md content as raw bytes.

    Layout:
        ## user
                                    (two blank lines as typing room)

        ## agent:<role-1>
                                    (one blank line)
        _(no reply from <role-1> yet)_
                                    (one blank line)
        ## agent:<role-2>
        ...

    The placeholder line is stable across cycles UNTIL the agent
    replaces it; agents that have nothing new to say leave it.
    Different roles get different placeholders so diff3 sees them as
    distinct unchanged lines for hunk-boundary purposes.
    """
    lines = [b"## user", b"", b""]
    for role in roles:
        lines.extend([
            f"## agent:{role}".encode("utf-8"),
            b"",
            f"_(no reply from {role} yet)_".encode("utf-8"),
            b"",
        ])
    # Trailing newline so the file ends with `\n` on disk.
    return b"\n".join(lines) + b"\n"
