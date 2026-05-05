"""Tests for chorale.backends -- adapters, custom factory, role-spec parser."""
from __future__ import annotations

import pytest

from chorale.backends import (
    BackendError,
    builtin_backends,
    make_custom_backend,
    parse_role_spec,
    resolve_backend,
)


# -- built-in argv shapes ------------------------------------------------

def test_builtin_claude_argv():
    b = builtin_backends()["claude"]
    cmd, stdin = b.build_cmd("hi", "claude-sonnet-4-6")
    assert cmd == ["claude", "--print", "-p", "hi", "--model", "claude-sonnet-4-6"]
    assert stdin is None


def test_builtin_claude_argv_no_model():
    b = builtin_backends()["claude"]
    cmd, stdin = b.build_cmd("hi", None)
    assert cmd == ["claude", "--print", "-p", "hi"]
    assert stdin is None


def test_builtin_gemini_argv():
    b = builtin_backends()["gemini"]
    cmd, stdin = b.build_cmd("hi", "gemini-2.5-pro")
    assert cmd == ["gemini", "-p", "hi", "--model", "gemini-2.5-pro"]
    assert stdin is None


def test_builtin_codex_argv():
    b = builtin_backends()["codex"]
    cmd, stdin = b.build_cmd("hi", "gpt-5-codex")
    assert cmd == ["codex", "exec", "hi", "--model", "gpt-5-codex"]
    assert stdin is None


def test_builtin_ollama_uses_stdin_for_prompt():
    """Ollama is the odd one out: model is positional, prompt via stdin."""
    b = builtin_backends()["ollama"]
    cmd, stdin = b.build_cmd("hi", "llama3")
    assert cmd == ["ollama", "run", "llama3"]
    assert stdin == b"hi"


def test_builtin_ollama_default_model_when_none():
    b = builtin_backends()["ollama"]
    cmd, stdin = b.build_cmd("hi", None)
    # Build defaults to llama3 when model is None.
    assert cmd == ["ollama", "run", "llama3"]
    assert stdin == b"hi"


def test_builtin_binaries_match_first_argv_token():
    """The Backend.binary used for dependency checks should match the
    actual executable each adapter invokes."""
    for name, b in builtin_backends().items():
        argv, _ = b.build_cmd("p", "m")
        assert argv[0] == b.binary, name


# -- custom backend factory ----------------------------------------------

def test_custom_argv_substitutes_placeholders():
    b = make_custom_backend(
        "my",
        ["my-tool", "--prompt={prompt}", "--model={model}"],
        prompt_via="argv",
        default_model="v1",
    )
    cmd, stdin = b.build_cmd("hello", "v2")
    assert cmd == ["my-tool", "--prompt=hello", "--model=v2"]
    assert stdin is None


def test_custom_stdin_pipes_prompt():
    b = make_custom_backend(
        "my",
        ["my-tool", "--model={model}"],
        prompt_via="stdin",
        default_model="v1",
    )
    cmd, stdin = b.build_cmd("hello", "v9")
    assert cmd == ["my-tool", "--model=v9"]
    assert stdin == b"hello"


def test_custom_default_model_used_when_none():
    """Backend.call's `model` arg is None -> default_model is used."""
    b = make_custom_backend(
        "my",
        ["my-tool", "--model={model}"],
        default_model="v1",
    )
    # build_cmd takes the resolved model; .call() does the resolution.
    cmd, _ = b.build_cmd("p", b.default_model)
    assert cmd == ["my-tool", "--model=v1"]


def test_custom_rejects_empty_template():
    with pytest.raises(BackendError):
        make_custom_backend("my", [])


def test_custom_rejects_bad_prompt_via():
    with pytest.raises(BackendError):
        make_custom_backend("my", ["x"], prompt_via="invalid")


def test_custom_binary_inferred_from_first_token():
    b = make_custom_backend("my", ["my-tool", "--prompt={prompt}"])
    assert b.binary == "my-tool"


# -- role-spec parsing ---------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("cook",                       ("cook", None,    None)),
    ("cook@gemini",                ("cook", "gemini", None)),
    ("cook@gemini:gemini-2.5-pro", ("cook", "gemini", "gemini-2.5-pro")),
    ("ux-designer@codex:gpt-5",    ("ux-designer", "codex", "gpt-5")),
    ("note-taker@my-local",        ("note-taker", "my-local", None)),
])
def test_parse_role_spec_valid(spec, expected):
    rs = parse_role_spec(spec)
    assert (rs.role, rs.backend, rs.model) == expected


@pytest.mark.parametrize("spec", [
    "",                  # empty
    "@gemini",           # no role
    "1bad",              # role starting with digit
    "cook@",             # backend empty
    "cook@@gemini",      # double @
    "cook@gem ini",      # space in backend
    "cook@gemini:",      # model empty
])
def test_parse_role_spec_invalid(spec):
    with pytest.raises(ValueError):
        parse_role_spec(spec)


def test_parse_role_spec_model_can_contain_colons_and_slashes():
    """Model names like `provider/model:tag` should pass through."""
    rs = parse_role_spec("r@b:openai/gpt-5:turbo")
    assert rs.role == "r"
    assert rs.backend == "b"
    assert rs.model == "openai/gpt-5:turbo"


# -- resolve_backend -----------------------------------------------------

def test_resolve_default_backend_no_model():
    registry = builtin_backends()
    spec = parse_role_spec("cook")
    b, m = resolve_backend(spec, registry, "claude", None)
    assert b.name == "claude"
    assert m is None  # falls through to backend.default_model in .call()


def test_resolve_default_backend_cli_default_model():
    """--default-model applies only when role has no @backend."""
    registry = builtin_backends()
    spec = parse_role_spec("cook")
    b, m = resolve_backend(spec, registry, "claude", "claude-haiku-4-5")
    assert b.name == "claude"
    assert m == "claude-haiku-4-5"


def test_resolve_explicit_backend_ignores_cli_default_model():
    """role@backend MUST NOT pick up --default-model -- that flag is
    tied to the *default* backend; an explicit backend uses its own
    default_model unless the spec says :MODEL."""
    registry = builtin_backends()
    spec = parse_role_spec("cook@gemini")
    b, m = resolve_backend(spec, registry, "claude", "claude-haiku-4-5")
    assert b.name == "gemini"
    assert m is None  # falls through to gemini's own default_model


def test_resolve_explicit_backend_with_model():
    registry = builtin_backends()
    spec = parse_role_spec("cook@gemini:gemini-2.5-pro")
    b, m = resolve_backend(spec, registry, "claude", None)
    assert b.name == "gemini"
    assert m == "gemini-2.5-pro"


def test_resolve_unknown_backend_raises():
    registry = builtin_backends()
    spec = parse_role_spec("cook@not-a-backend")
    with pytest.raises(ValueError, match="unknown backend"):
        resolve_backend(spec, registry, "claude", None)
