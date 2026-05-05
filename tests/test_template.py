"""Tests for chorale.template."""
from __future__ import annotations

from chorale.template import render_template


def test_render_template_layout():
    out = render_template(["alice", "bob"]).decode("utf-8")
    assert out.startswith("## user\n\n\n## agent:alice\n")
    assert out.endswith("\n")
    assert "_(no reply from alice yet)_" in out
    assert "_(no reply from bob yet)_" in out


def test_render_template_unique_placeholders():
    """Per-role placeholder lines are unique -- diff3 needs distinct
    unchanged anchors between sections to avoid spurious conflicts."""
    out = render_template(["alpha", "beta", "gamma"]).decode("utf-8")
    placeholders = [
        "_(no reply from alpha yet)_",
        "_(no reply from beta yet)_",
        "_(no reply from gamma yet)_",
    ]
    for p in placeholders:
        assert out.count(p) == 1, f"{p!r} should appear exactly once"


def test_render_template_no_roles():
    out = render_template([])
    # Just the user header + blank lines, ending with newline.
    assert out == b"## user\n\n\n"


def test_render_template_section_count():
    """N roles produce N+1 section headers (## user + N agents)."""
    out = render_template(["a", "b", "c", "d"])
    headers = [line for line in out.split(b"\n") if line.startswith(b"## ")]
    assert len(headers) == 5
    assert headers[0] == b"## user"
    assert headers[1:] == [
        b"## agent:a",
        b"## agent:b",
        b"## agent:c",
        b"## agent:d",
    ]
