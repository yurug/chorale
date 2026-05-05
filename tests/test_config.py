"""Tests for chorale.config -- TOML loading + registry assembly."""
from __future__ import annotations

from pathlib import Path

import pytest

from chorale.config import Config, ConfigError, build_registry, load_config


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_file_returns_defaults(tmp_path: Path):
    cfg = load_config(tmp_path / "no-such-file.toml")
    assert cfg.default_backend == "claude"
    assert cfg.default_model is None
    assert cfg.backend_overrides == {}


def test_invalid_toml_raises(tmp_path: Path):
    p = write(tmp_path, "this is not = valid toml [[[")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(p)


def test_defaults_section(tmp_path: Path):
    p = write(tmp_path, """
[defaults]
backend = "gemini"
model   = "gemini-2.5-pro"
""")
    cfg = load_config(p)
    assert cfg.default_backend == "gemini"
    assert cfg.default_model == "gemini-2.5-pro"


def test_override_builtin_default_model(tmp_path: Path):
    p = write(tmp_path, """
[backends.gemini]
default_model = "gemini-2.5-pro"
""")
    cfg = load_config(p)
    registry = build_registry(cfg)
    assert registry["gemini"].default_model == "gemini-2.5-pro"
    # Other built-ins untouched.
    assert registry["claude"].default_model == "claude-sonnet-4-6"


def test_define_custom_backend(tmp_path: Path):
    p = write(tmp_path, """
[backends.my-local]
command       = ["my-tool", "--prompt={prompt}", "--model={model}"]
prompt_via    = "argv"
default_model = "v1"
timeout       = 90.0
""")
    cfg = load_config(p)
    registry = build_registry(cfg)
    assert "my-local" in registry
    b = registry["my-local"]
    assert b.name == "my-local"
    assert b.binary == "my-tool"
    assert b.default_model == "v1"
    assert b.timeout == 90.0
    cmd, stdin = b.build_cmd("hello", "v2")
    assert cmd == ["my-tool", "--prompt=hello", "--model=v2"]
    assert stdin is None


def test_custom_backend_stdin_mode(tmp_path: Path):
    p = write(tmp_path, """
[backends.stdin-tool]
command       = ["pipey", "--model={model}"]
prompt_via    = "stdin"
default_model = "x"
""")
    cfg = load_config(p)
    registry = build_registry(cfg)
    cmd, stdin = registry["stdin-tool"].build_cmd("hello", "x")
    assert cmd == ["pipey", "--model=x"]
    assert stdin == b"hello"


def test_custom_backend_missing_command_raises(tmp_path: Path):
    p = write(tmp_path, """
[backends.broken]
prompt_via = "argv"
""")
    cfg = load_config(p)
    with pytest.raises(ConfigError, match="must be a list of strings"):
        build_registry(cfg)


def test_custom_backend_bad_prompt_via_raises(tmp_path: Path):
    p = write(tmp_path, """
[backends.broken]
command    = ["x"]
prompt_via = "carrier-pigeon"
""")
    cfg = load_config(p)
    with pytest.raises(ConfigError, match="prompt_via"):
        build_registry(cfg)


def test_full_example_round_trips(tmp_path: Path):
    """The README's example config should load and produce a sensible
    registry."""
    p = write(tmp_path, """
[defaults]
backend = "claude"
model   = "claude-sonnet-4-6"

[backends.gemini]
default_model = "gemini-2.5-pro"

[backends.my-local]
command       = ["my-tool", "--prompt={prompt}", "--model={model}"]
prompt_via    = "argv"
default_model = "v1"
""")
    cfg = load_config(p)
    assert cfg.default_backend == "claude"
    assert cfg.default_model == "claude-sonnet-4-6"

    registry = build_registry(cfg)
    assert set(registry) >= {"claude", "gemini", "codex", "ollama", "my-local"}
    assert registry["gemini"].default_model == "gemini-2.5-pro"
    assert registry["my-local"].binary == "my-tool"
