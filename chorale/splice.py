"""Section-aware splice for Markdown-shaped files -- the conflict-killer.

Why this exists
===============

The temptation when wiring up a multi-agent system is to ask each
LLM to produce the entire updated file, then trust whatever it gives
you and write that to disk. In practice every model -- including the
"good" ones -- will:

  - Reformat parts of the file you didn't mention (whitespace, list
    bullet style, code-fence languages).
  - "Helpfully" fix what it perceives as typos in someone else's
    section.
  - Add or remove blank lines around its own section, occasionally.
  - Drop or duplicate sections under prompt confusion.

In a single-agent setup that's annoying but recoverable. In a
multi-agent setup it's catastrophic: every spurious whitespace tweak
becomes a candidate diff3 conflict against another agent's
genuinely-disjoint edit, and the chorale grinds to a halt in
ConflictPending.

The fix: don't trust the LLM with anything outside its own section.

What this module does
=====================

`splice_section(base_bytes, agent_output, role)':

  1. Parse `base_bytes` (cotype's most recent base for FILE) into
     Markdown sections.
  2. Parse `agent_output` (what the LLM returned) the same way.
  3. Find the body of `## agent:<role>' in BOTH.
  4. If the agent's body equals the base's body, return None
     ("nothing to save"; the caller skips the cotype-save call).
  5. Otherwise, build a new sections list: identical to base in every
     section EXCEPT the agent's own, where we splice in the agent's
     fresh body.
  6. Reassemble bytes.

The result has the property that every byte outside the
`## agent:<role>' section is byte-exact identical to base_bytes.
Two agents editing two different sections produce splice outputs
that differ from base ONLY in their respective sections -- disjoint
byte ranges, by construction.

Section model
=============

A "section" is an entry in a flat list, where each entry is a list
of bytes-lines:

  - Sections start at every line that begins with `## '.
  - The first line of a section is the header line; the rest are
    its "body".
  - Lines before the first `## ' (a "preamble") form the first
    section. (We don't usually have one -- chorale's pre-allocated
    template starts with `## user' -- but the parser handles it
    gracefully.)

Pure functions: no filesystem, no subprocess, no global state. Easy
to unit-test (see `tests/test_splice.py').

Defensive: triple-backtick fence stripping
==========================================

Even when explicitly told NOT to wrap output in code fences, LLMs
sometimes do anyway. We strip a single layer of triple-backtick
fence at the start and end of `agent_output' before parsing.
Heuristic:

  - The output, after `.strip()`, must start with three backticks AND
    end with three backticks.
  - The first newline after the opening backticks is the boundary;
    everything between that newline and the closing fence is treated
    as the "real" content.

This catches both ```\\n...\\n``` and ```markdown\\n...\\n``` shapes.
We don't handle nested or partial fences -- those are uncommon and
the cost of a wrong strip would be higher than the benefit.
"""
from __future__ import annotations

from typing import List, Optional


# Outcome sentinel: an internal marker for the no-change case. We
# actually return `None' instead (which is more Pythonic and what
# tests check for); this constant is left as documentation of the
# "no semantic change" outcome.
NO_CHANGE = object()


def split_sections(b: bytes) -> List[List[bytes]]:
    """Split bytes into sections, each starting with a `## ' line.

    The "preamble" before the first `## ' line (if any) becomes the
    first section -- so a file like `# title\\n\\n## a\\n...' produces
    sections [`['# title', '']`, ['## a', ...]`].

    Each section is a list of trailing-newline-stripped lines.
    Round-trip is byte-exact:

        join_sections(split_sections(b)) == b

    for any `b'. (See `tests/test_splice.py::test_split_join_roundtrip`.)
    """
    sections: List[List[bytes]] = []
    current: List[bytes] = []
    for line in b.split(b"\n"):
        if line.startswith(b"## "):
            # New section header; flush the current section (if any)
            # and start a new one with this header line.
            if current:
                sections.append(current)
            current = [line]
        else:
            current.append(line)
    # Flush the last section.
    if current:
        sections.append(current)
    return sections


def join_sections(sections: List[List[bytes]]) -> bytes:
    """Inverse of `split_sections' -- reassemble bytes.

    The double-join (lines within section, sections within file)
    re-introduces exactly the newlines that `split_sections' removed.
    Order matters: sections are joined IN ORDER, lines IN ORDER --
    `splice_section' relies on this for byte-stability.
    """
    return b"\n".join(b"\n".join(s) for s in sections)


def find_section_body(
    sections: List[List[bytes]], header: bytes
) -> Optional[List[bytes]]:
    """Return the body lines of the first section whose header matches.

    The "body" is everything after the header (i.e. `section[1:]').
    Returns `None' if no section's header equals `header' exactly --
    this is the signal we use elsewhere to mean "the agent dropped
    this section" or "this base lacks the section".

    Header comparison is byte-equal: `## agent:cook' and `## agent:cook '
    (trailing space) are different. That's intentional -- a real
    diff3 marker like `<<<<<<< /tmp/...' starts with `## ' through a
    quirk of formatting? No, those start with `<<<<<<<', so this is
    fine. But the strict equality means a typo in the role name
    will make us return None instead of mis-targeting.
    """
    for s in sections:
        if s and s[0] == header:
            return s[1:]
    return None


def splice_section(
    base_bytes: bytes, agent_output: bytes, role: str
) -> Optional[bytes]:
    """Splice the agent's `## agent:<role>' section body into base_bytes.

    The contract:

      base_bytes outside the `## agent:<role>' section -> appears
      VERBATIM in the returned bytes.

      base_bytes inside the `## agent:<role>' section's body ->
      replaced with the body the agent wrote, EXTRACTED FROM the
      agent's own corresponding section.

    Returns:
      - new bytes, when the agent's section body in `agent_output'
        differs from the same section's body in `base_bytes'.
      - None, when there's nothing to save:
          * the agent's body equals the base's body (no semantic
            change); OR
          * the agent dropped its own section entirely from its
            output; OR
          * `base_bytes' somehow lacks the section (shouldn't happen
            with the pre-allocated template, but defensive).

    Caller (run.py) treats `None' as "skip this iteration" -- no
    cotype-save call, no churn.

    Defensive: triple-backtick fence stripping -- see module docstring.
    """
    header = f"## agent:{role}".encode("utf-8")

    # Strip a single layer of triple-backtick fence if the agent
    # wrapped its whole output. We do the strip BEFORE splitting into
    # sections so the wrapper doesn't accidentally end up in the
    # parsed structure.
    stripped = agent_output.strip()
    if stripped.startswith(b"```") and stripped.endswith(b"```"):
        # Drop the leading ``` and the trailing ```; what's between
        # the first newline-after-leading-fence and the closing fence
        # is the real content.
        inner = stripped[3:-3]
        nl = inner.find(b"\n")
        if nl >= 0:
            agent_output = inner[nl + 1:]
        # If there's no newline (the whole thing was on one line, like
        # ```just-this```), we can't reliably identify the language
        # tag boundary; fall through and let parsing handle it.

    base_secs = split_sections(base_bytes)
    agent_secs = split_sections(agent_output)

    base_body = find_section_body(base_secs, header)
    agent_body = find_section_body(agent_secs, header)

    # No-change paths. Either side missing the section -> we have
    # nothing to splice; equal bodies -> nothing changed. In all
    # three cases the right answer is "skip the save".
    if base_body is None or agent_body is None:
        return None
    if agent_body == base_body:
        return None

    # The splice itself. List comprehension preserves order:
    # for the agent's own section, replace the body; for every
    # other section, pass through unchanged.
    out = [
        ([s[0]] + agent_body) if (s and s[0] == header) else s
        for s in base_secs
    ]
    return join_sections(out)
