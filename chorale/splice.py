"""Section-aware splice for Markdown-shaped files.

The harness does NOT trust an LLM agent's full-file output byte-for-byte.
Given (base_bytes, agent_output_bytes, role), this module extracts ONLY
the body of `## agent:<role>` from the agent's output and splices it
into base_bytes, replacing that section's body. Every byte outside the
agent's section is byte-exact identical to base_bytes.

Consequence: two agents editing two different `## agent:<role>` sections
produce edits in disjoint regions of the byte stream, so cotype's 3-way
merge cannot conflict between them by construction.

Pure functions. No filesystem, no subprocess. Easy to unit-test.
"""
from __future__ import annotations

from typing import List, Optional


# Outcome sentinel returned by `splice_section` when the result equals
# the base (no semantic change for this agent).
NO_CHANGE = object()


def split_sections(b: bytes) -> List[List[bytes]]:
    """Split bytes into sections, each starting with a `## ` line.

    A 'preamble' (lines before the first `## ` line) becomes the first
    section. Each section is a list of trailing-newline-stripped lines;
    `b"\\n".join(b"\\n".join(s) for s in sections)` round-trips bytes.
    """
    sections: List[List[bytes]] = []
    current: List[bytes] = []
    for line in b.split(b"\n"):
        if line.startswith(b"## "):
            if current:
                sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    return sections


def join_sections(sections: List[List[bytes]]) -> bytes:
    """Inverse of `split_sections` -- reassemble bytes."""
    return b"\n".join(b"\n".join(s) for s in sections)


def find_section_body(
    sections: List[List[bytes]], header: bytes
) -> Optional[List[bytes]]:
    """Return the body lines of the first section whose header matches.

    The 'body' is everything after the header line (which is line[0]).
    Returns None if no matching section exists.
    """
    for s in sections:
        if s and s[0] == header:
            return s[1:]
    return None


def splice_section(
    base_bytes: bytes, agent_output: bytes, role: str
) -> Optional[bytes]:
    """Splice the agent's `## agent:<role>` section body into base_bytes.

    Returns:
      - new bytes, when the agent's section body in `agent_output`
        differs from the same section's body in `base_bytes`.
      - None, when there's no semantic change (agent's body equals
        base's body, OR the agent dropped its own section, OR the base
        somehow lacks the section). Caller should skip the save.

    Defensive: strips a single layer of ``` ... ``` fence if the agent
    wrapped its whole output (Claude does this even when told not to).
    """
    header = f"## agent:{role}".encode("utf-8")

    # Strip a single layer of triple-backtick fence if present.
    stripped = agent_output.strip()
    if stripped.startswith(b"```") and stripped.endswith(b"```"):
        inner = stripped[3:-3]
        nl = inner.find(b"\n")
        if nl >= 0:
            agent_output = inner[nl + 1 :]

    base_secs = split_sections(base_bytes)
    agent_secs = split_sections(agent_output)

    base_body = find_section_body(base_secs, header)
    agent_body = find_section_body(agent_secs, header)

    if base_body is None or agent_body is None:
        return None
    if agent_body == base_body:
        return None

    out = [
        ([s[0]] + agent_body) if (s and s[0] == header) else s
        for s in base_secs
    ]
    return join_sections(out)
