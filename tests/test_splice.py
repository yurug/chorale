"""Tests for chorale.splice."""
from __future__ import annotations

from chorale.splice import (
    find_section_body,
    join_sections,
    splice_section,
    split_sections,
)
from chorale.template import render_template


# -- low-level round-trip ------------------------------------------------

def test_split_join_roundtrip_byte_exact():
    """split + join must be the identity on arbitrary bytes."""
    cases = [
        b"",
        b"## user\n\nbody\n",
        b"# preamble\n\n## a\nbody-a\n\n## b\nbody-b\n",
        # No trailing newline.
        b"## a\nbody-a",
    ]
    for b in cases:
        assert join_sections(split_sections(b)) == b


def test_find_section_body_returns_lines_after_header():
    bytes_ = b"## a\nx\ny\n\n## b\nz\n"
    secs = split_sections(bytes_)
    assert find_section_body(secs, b"## a") == [b"x", b"y", b""]
    assert find_section_body(secs, b"## b") == [b"z", b""]
    assert find_section_body(secs, b"## missing") is None


# -- splice_section: the actual contract ---------------------------------

BASE = render_template(["alice", "bob", "carol"])


def test_splice_replaces_only_own_section():
    """Other sections must come from base byte-exact."""
    agent_output = (
        b"## user\n\n[ignored]\n\n"
        b"## agent:alice\n\nAlice's real reply.\n\n"
        b"## agent:bob\n\n[ignored junk]\n\n"
        b"## agent:carol\n\n[ignored]\n"
    )
    out = splice_section(BASE, agent_output, "alice")
    assert out is not None
    text = out.decode("utf-8")

    # Alice's section was replaced with the new body.
    assert "Alice's real reply." in text
    # Bob's and Carol's sections kept their original placeholders --
    # whatever the agent wrote there is discarded.
    assert "_(no reply from bob yet)_" in text
    assert "_(no reply from carol yet)_" in text
    assert "[ignored]" not in text
    assert "[ignored junk]" not in text


def test_splice_returns_none_on_no_change():
    """If the agent's section body equals base's, signal no-change."""
    out = splice_section(BASE, BASE, "alice")
    assert out is None


def test_splice_returns_none_when_role_missing_from_agent_output():
    """Agent dropped its own section; treat as no change."""
    truncated = (
        b"## user\n\n\n"
        b"## agent:bob\n\nbob speaks\n\n"
    )  # no agent:alice section
    assert splice_section(BASE, truncated, "alice") is None


def test_splice_returns_none_when_role_missing_from_base():
    """Base lacks the section; nothing to splice into."""
    base_no_alice = b"## user\n\n\n## agent:bob\n\nbody\n"
    agent_output = b"## agent:alice\n\nhi\n"
    assert splice_section(base_no_alice, agent_output, "alice") is None


def test_splice_strips_codefence_wrap():
    """Claude sometimes wraps the whole response in ``` despite instructions."""
    fenced = (
        b"```markdown\n"
        b"## user\n\n\n"
        b"## agent:alice\n\nFenced reply.\n\n"
        b"## agent:bob\n\n[junk]\n\n"
        b"## agent:carol\n\n[junk]\n"
        b"```"
    )
    out = splice_section(BASE, fenced, "alice")
    assert out is not None
    assert b"Fenced reply." in out
    # Codefence markers must not leak into the saved bytes.
    assert b"```" not in out


def test_splice_two_disjoint_agents_dont_collide():
    """The whole point of the splicer: alice and bob produce disjoint
    edits that compose by concatenating their splice results."""
    alice_output = (
        b"## agent:alice\n\nAlice speaks.\n\n"
    )
    bob_output = (
        b"## agent:bob\n\nBob speaks.\n\n"
    )
    after_alice = splice_section(BASE, alice_output, "alice")
    assert after_alice is not None
    after_both = splice_section(after_alice, bob_output, "bob")
    assert after_both is not None

    text = after_both.decode("utf-8")
    assert "Alice speaks." in text
    assert "Bob speaks." in text
    # Carol's placeholder unchanged through both splices.
    assert "_(no reply from carol yet)_" in text
