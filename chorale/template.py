"""Pre-allocate a Markdown section template for the user + N agents.

What this module is for
=======================

When a chorale is started on an empty file, run() calls
`render_template(roles)' to seed the file with a layout that the
splicer (in `chorale.splice') and the diff3 merge engine (inside
cotype) both want to see:

    ## user


    ## agent:cook

    _(no reply from cook yet)_

    ## agent:logistics

    _(no reply from logistics yet)_

    ## agent:ux-designer

    _(no reply from ux-designer yet)_

    ...

Why this layout, in detail
==========================

(1) `## user' is first.

    The user types under it; agents read it for context. Putting it
    at the top makes the document read top-to-bottom as a
    conversation: question first, then answers.

(2) Two blank lines after `## user' before the next header.

    That's room for the user to type a paragraph or two without
    immediately bumping into the next section. The puppeteer in the
    demos (`examples/demo-crepe-nvim/bg-user.py') puts its messages
    in there; a real human user does too.

(3) Each `## agent:<role>' section gets a UNIQUE placeholder line:

        _(no reply from <role> yet)_

    This is the diff3-anchor trick. cotype's merge is line-based
    POSIX `diff3', which conflates adjacent edits into the same
    hunk: if `## agent:A' and `## agent:B' are next to each other
    with no unchanged lines between them, edits to A and B can
    spuriously conflict.

    Per-role placeholders give diff3 stable per-section anchors.
    When agent A's placeholder is replaced with a real reply, agent
    B's placeholder is unchanged -- diff3 sees a unique unchanged
    line (`_(no reply from B yet)_') between A and the next agent
    section, and treats A's edit as its own hunk.

    Without the per-role suffix, every agent section's placeholder
    would be IDENTICAL (e.g. just `_(no reply yet)_'), and diff3
    couldn't tell them apart for hunk-grouping purposes -- the
    boundary anchor degenerates to "any blank line", which the
    edits would inevitably consume.

(4) One blank line above + one below each placeholder.

    Light visual breathing room. Could be more (which would give
    diff3 more anchor lines), but the unique placeholder text is
    already enough; extra blank lines would just make the file
    longer to read.

(5) File ends with `\\n' (one trailing newline).

    Standard POSIX text-file convention. The `b"\\n".join(...)`
    composition + the explicit `+ b"\\n"' at the end produce
    exactly one trailing newline.

Pure function: no filesystem, no I/O. The caller (run.run()) decides
where to write the bytes.
"""
from __future__ import annotations

from typing import Iterable


def render_template(roles: Iterable[str]) -> bytes:
    """Render the initial brainstorm.md content as raw bytes.

    Layout (roles=['cook', 'logistics']):

        ## user
                                    (line 2: blank, typing room)
                                    (line 3: blank, more typing room)
        ## agent:cook
                                    (blank, anchor)
        _(no reply from cook yet)_
                                    (blank, anchor)
        ## agent:logistics
                                    (blank, anchor)
        _(no reply from logistics yet)_
                                    (blank, anchor)

    The unique-per-role placeholder line is what makes adjacent
    agent edits not conflict in diff3 -- see the module docstring.

    Args:
        roles: an iterable of role names (strings). They become
               the suffix of `## agent:<role>' headers.

    Returns:
        The full rendered bytes, with one trailing `\\n'.
    """
    # `## user' starts the file with two blank lines as typing room.
    lines = [b"## user", b"", b""]
    for role in roles:
        # For each agent: header, blank, placeholder, blank.
        # The blank-placeholder-blank sandwich gives diff3 anchors
        # both above and below the placeholder.
        lines.extend([
            f"## agent:{role}".encode("utf-8"),
            b"",
            f"_(no reply from {role} yet)_".encode("utf-8"),
            b"",
        ])
    # Trailing newline so the file ends with `\n` on disk.
    return b"\n".join(lines) + b"\n"
