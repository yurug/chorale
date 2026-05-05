"""Adapter layer for multiple AI-backend CLIs.

A `Backend` wraps the per-CLI quirks of how to:
  - Build the argv list for a given prompt + model.
  - Pass the prompt (via argv or stdin).
  - Fall back to a sensible default model.

Built-ins: claude (default), gemini, codex, ollama. Custom backends can
be defined in the config file (see chorale.config).

The role-spec parser lives here too: a role string is one of
  - `name`                       (default backend, default model)
  - `name@backend`               (specific backend, its default model)
  - `name@backend:model`         (specific backend, specific model)
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


# A `BuildCmd` takes (prompt, model) and returns (argv, stdin_bytes_or_None).
BuildCmd = Callable[[str, Optional[str]], Tuple[List[str], Optional[bytes]]]


class BackendError(Exception):
    """Configuration / shape error for a backend definition."""


@dataclass
class Backend:
    """One backend = one tiny CLI adapter."""

    name: str
    build_cmd: BuildCmd
    binary: str  # the executable that needs to be on PATH
    default_model: Optional[str] = None
    timeout: float = 60.0

    def call(self, prompt: str, model: Optional[str] = None) -> Optional[bytes]:
        """Invoke the backend; return stdout bytes or None on failure."""
        m = model or self.default_model
        argv, stdin = self.build_cmd(prompt, m)
        try:
            r = subprocess.run(
                argv, input=stdin, capture_output=True,
                check=False, timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        return r.stdout if r.returncode == 0 else None


# -- built-in adapters ---------------------------------------------------

def _claude_cmd(prompt: str, model: Optional[str]) -> Tuple[List[str], Optional[bytes]]:
    cmd = ["claude", "--print", "-p", prompt]
    if model:
        cmd += ["--model", model]
    return cmd, None


def _gemini_cmd(prompt: str, model: Optional[str]) -> Tuple[List[str], Optional[bytes]]:
    cmd = ["gemini", "-p", prompt]
    if model:
        cmd += ["--model", model]
    return cmd, None


def _codex_cmd(prompt: str, model: Optional[str]) -> Tuple[List[str], Optional[bytes]]:
    cmd = ["codex", "exec", prompt]
    if model:
        cmd += ["--model", model]
    return cmd, None


def _ollama_cmd(prompt: str, model: Optional[str]) -> Tuple[List[str], Optional[bytes]]:
    # Ollama: model is positional, prompt comes via stdin.
    cmd = ["ollama", "run", model or "llama3"]
    return cmd, prompt.encode("utf-8")


def builtin_backends() -> Dict[str, Backend]:
    """Fresh dict of built-in adapters; safe for the caller to mutate."""
    return {
        "claude": Backend(
            name="claude",
            build_cmd=_claude_cmd,
            binary="claude",
            default_model="claude-sonnet-4-6",
        ),
        "gemini": Backend(
            name="gemini",
            build_cmd=_gemini_cmd,
            binary="gemini",
            default_model=None,  # let the gemini CLI pick its own default
        ),
        "codex": Backend(
            name="codex",
            build_cmd=_codex_cmd,
            binary="codex",
            default_model=None,
        ),
        "ollama": Backend(
            name="ollama",
            build_cmd=_ollama_cmd,
            binary="ollama",
            default_model="llama3",
        ),
    }


# -- custom backend factory ----------------------------------------------

def make_custom_backend(
    name: str,
    command_template: List[str],
    prompt_via: str = "argv",
    default_model: Optional[str] = None,
    timeout: float = 60.0,
) -> Backend:
    """Build a Backend from a config-defined command template.

    `command_template` is a list of tokens; each token can contain
    `{prompt}` and/or `{model}` placeholders, expanded per call. When
    `prompt_via == "argv"`, `{prompt}` substitutes the prompt directly
    into the argv. When `prompt_via == "stdin"`, the prompt is piped
    into the subprocess's stdin and any `{prompt}` placeholder in the
    template substitutes the empty string -- so users typically just
    don't include `{prompt}` in stdin-mode templates.

    The first token (post-substitution) is treated as the executable
    name for dependency checks. Templating in the first token is
    discouraged; if you do it, the resulting binary name is whatever
    formats out for an empty prompt + empty model.
    """
    if prompt_via not in ("argv", "stdin"):
        raise BackendError(
            f"backend {name!r}: prompt_via must be 'argv' or 'stdin', "
            f"got {prompt_via!r}"
        )
    if not command_template:
        raise BackendError(
            f"backend {name!r}: command_template must not be empty"
        )

    def build(prompt: str, model: Optional[str]) -> Tuple[List[str], Optional[bytes]]:
        argv: List[str] = []
        prompt_value = prompt if prompt_via == "argv" else ""
        model_value = model or ""
        for tok in command_template:
            try:
                argv.append(tok.format(prompt=prompt_value, model=model_value))
            except (KeyError, IndexError) as e:
                raise BackendError(
                    f"backend {name!r}: bad placeholder in token {tok!r}: {e}"
                ) from e
        stdin = prompt.encode("utf-8") if prompt_via == "stdin" else None
        return argv, stdin

    # Best-effort: derive the binary name from the first token with empty
    # placeholders. Used only for the dependency check.
    try:
        binary = command_template[0].format(prompt="", model="").split()[0]
    except (KeyError, IndexError, ValueError):
        binary = command_template[0]

    return Backend(
        name=name,
        build_cmd=build,
        binary=binary,
        default_model=default_model,
        timeout=timeout,
    )


# -- role-spec parsing ---------------------------------------------------

# `name` | `name@backend` | `name@backend:model`. Names are alphanumeric
# plus `_` and `-`; the `:model` tail is freeform (anything but `@`).
_ROLE_SPEC_RE = re.compile(
    r"^(?P<role>[A-Za-z][A-Za-z0-9_\-]*)"
    r"(?:@(?P<backend>[A-Za-z][A-Za-z0-9_\-]*)"
    r"(?::(?P<model>[^@]+))?)?$"
)


@dataclass
class RoleSpec:
    role: str
    backend: Optional[str] = None
    model: Optional[str] = None


def parse_role_spec(spec: str) -> RoleSpec:
    """Parse `name` / `name@backend` / `name@backend:model`.

    Raises ValueError on a malformed spec.
    """
    m = _ROLE_SPEC_RE.match(spec)
    if not m:
        raise ValueError(
            f"invalid role spec {spec!r}: expected NAME, NAME@BACKEND, "
            f"or NAME@BACKEND:MODEL"
        )
    return RoleSpec(
        role=m.group("role"),
        backend=m.group("backend"),
        model=m.group("model"),
    )


def resolve_backend(
    spec: RoleSpec,
    registry: Dict[str, Backend],
    default_backend: str,
    cli_default_model: Optional[str],
) -> Tuple[Backend, Optional[str]]:
    """Resolve a parsed RoleSpec to (backend, model_override).

    Backend selection: `spec.backend` if set, else `default_backend`.

    Model selection (in order of priority):
      1. spec.model (the `:MODEL` tail of the spec).
      2. cli_default_model, ONLY when spec.backend is unset (so the
         CLI's --default-model applies to the default backend, not to
         every backend the user wires in via `@other`).
      3. None -- meaning Backend.call() falls through to the Backend's
         own `default_model`.
    """
    backend_name = spec.backend or default_backend
    if backend_name not in registry:
        raise ValueError(
            f"unknown backend {backend_name!r} for role {spec.role!r}; "
            f"known backends: {sorted(registry)}"
        )
    backend = registry[backend_name]

    if spec.model is not None:
        model_override: Optional[str] = spec.model
    elif spec.backend is None and cli_default_model is not None:
        model_override = cli_default_model
    else:
        model_override = None

    return backend, model_override
