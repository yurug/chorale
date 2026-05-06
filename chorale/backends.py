"""Adapter layer for multiple AI-backend CLIs.

Why backends are CLI subprocesses, not SDK calls
================================================

Every "backend" in chorale is a thin adapter around an external CLI:
`claude --print -p ...', `gemini -p ...', `ollama run ...', or a
user-defined command from the TOML config. We deliberately do NOT
import `anthropic', `google-generativeai', or `openai' SDKs.

The reasoning:

  - **Authentication is the user's**. They've already configured
    `claude auth' / `gemini auth' / `ollama serve'. Chorale inherits
    those credentials by spawning the CLI as the user; no new
    secret-handling story.

  - **Provider-specific shaping (rate limits, JSON modes, system
    prompts) is the CLI's job**, not ours. When a provider ships an
    update, the CLI updates; chorale doesn't have to.

  - **Adding a new provider becomes one entry** in
    `BUILTIN_BACKENDS' or one TOML section, not a new SDK
    dependency.

The cost is per-call subprocess overhead -- ~0.5-1s for the
API-backed CLIs, negligible for `ollama' (which talks to a local
daemon). For multi-second LLM calls that's a rounding error.

What this module exposes
========================

  Backend                -- a small dataclass: `name', `binary',
                            `default_model', `timeout', and a
                            `build_cmd' callable that turns
                            (prompt, model) into (argv, stdin_bytes).
  builtin_backends()     -- fresh dict of the four built-ins:
                            claude, gemini, codex, ollama.
  make_custom_backend()  -- factory for config-defined backends from
                            a `[backends.NAME]' TOML section.
  parse_role_spec()      -- parse `name', `name@backend', or
                            `name@backend:model'.
  resolve_backend()      -- resolve a parsed `RoleSpec' to a
                            (Backend, model) pair, applying the
                            default-backend / default-model rules.

The four built-ins encode a per-CLI quirk
=========================================

Each built-in adapter knows the right invocation shape for its CLI.
This is where the per-provider differences live, isolated from the
rest of the codebase:

  claude   -- `claude --print -p PROMPT --model MODEL'
              prompt: argv,    model: --model flag
  gemini   -- `gemini -p PROMPT --model MODEL'
              prompt: argv,    model: --model flag
  codex    -- `codex exec PROMPT --model MODEL'
              prompt: argv,    model: --model flag
  ollama   -- `ollama run MODEL' (prompt via stdin)
              prompt: stdin,   model: positional

Three of the four put the prompt on argv; ollama is the odd one
out. The `Backend.call()' method handles both shapes uniformly via
the (argv, stdin) tuple returned from `build_cmd'.

Subprocess error semantics
==========================

`Backend.call()' returns `Optional[bytes]':

  - bytes (possibly empty) on a successful subprocess (exit 0).
  - None on TimeoutExpired, FileNotFoundError, OSError, or non-zero
    exit. We classify all of these the same way at the call site
    (`run._agent_loop` logs and skips); distinguishing "timed out"
    from "binary missing" doesn't change what the agent loop does
    next.

A tighter taxonomy could be useful for ops dashboards (e.g. "did
this provider time out a lot today?"), but chorale today writes log
lines, not metrics, and the simpler return type wins.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


# A `BuildCmd' takes (prompt, model) and returns (argv, stdin_bytes_or_None).
# argv is what `subprocess.run' is given; stdin_bytes is piped to the
# subprocess's stdin (or None for "no stdin"). Splitting these is the
# key abstraction that lets us accommodate ollama's stdin-prompt model
# alongside the other CLIs' argv-prompt model uniformly.
BuildCmd = Callable[[str, Optional[str]], Tuple[List[str], Optional[bytes]]]


class BackendError(Exception):
    """Configuration / shape error for a backend definition.

    Raised by `make_custom_backend' when a TOML-defined backend is
    malformed: empty `command', invalid `prompt_via', bad placeholder
    in a token. Distinct from `ConfigError' (in config.py), which
    wraps the broader "TOML file is broken" cases.
    """


@dataclass
class Backend:
    """One backend = one tiny CLI adapter.

    Fields:
        name           Human-readable identifier; used in role specs
                       (`role@<name>') and log lines.
        build_cmd      Callable that builds the subprocess invocation;
                       see `BuildCmd' alias above.
        binary         The executable that needs to be on PATH for
                       this backend to work. Used by
                       `run.check_dependencies' to error out cleanly
                       when a needed CLI is missing.
        default_model  Model passed if the role spec didn't specify
                       one. None means "let the CLI pick its default".
        timeout        Subprocess timeout in seconds. Generous default
                       (60s) because some providers can be slow on
                       cold-start.
    """

    name: str
    build_cmd: BuildCmd
    binary: str
    default_model: Optional[str] = None
    timeout: float = 60.0

    def call(self, prompt: str, model: Optional[str] = None) -> Optional[bytes]:
        """Invoke the backend; return stdout bytes or None on failure.

        Args:
            prompt -- the rendered prompt template (already processed
                      by `prompt.render_prompt').
            model  -- per-role model override; if None, falls through
                      to `self.default_model'.

        Returns:
            bytes  -- the subprocess's stdout if it exited 0.
            None   -- on timeout, missing binary, OS error, or
                      non-zero exit. Caller logs and moves on.
        """
        # `model or self.default_model' lets None propagate ALL the way
        # through to the build_cmd, where each adapter decides whether
        # to omit the `--model' flag entirely or fall back further.
        m = model or self.default_model
        argv, stdin = self.build_cmd(prompt, m)
        try:
            r = subprocess.run(
                argv, input=stdin, capture_output=True,
                check=False, timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            # All three failure modes get the same treatment: return
            # None and let the caller log + skip. We don't raise
            # because that would cascade through the threaded agent
            # loop in awkward ways.
            return None
        return r.stdout if r.returncode == 0 else None


# -- built-in adapters ---------------------------------------------------
#
# Each `_xxx_cmd' encodes one CLI's "how to invoke me" knowledge.
# Order in argv matters per-CLI (some accept --model before the
# prompt, others after); these match each tool's documented usage.

def _claude_cmd(
    prompt: str, model: Optional[str]
) -> Tuple[List[str], Optional[bytes]]:
    """Claude Code CLI: `claude --print -p PROMPT [--model MODEL]'.

    `--print' makes claude output the response and exit, instead of
    entering its interactive REPL. Prompt goes on argv (the CLI does
    NOT read stdin for `--print -p' mode).
    """
    cmd = ["claude", "--print", "-p", prompt]
    if model:
        cmd += ["--model", model]
    return cmd, None


def _gemini_cmd(
    prompt: str, model: Optional[str]
) -> Tuple[List[str], Optional[bytes]]:
    """Google Gemini CLI: `gemini -p PROMPT [--model MODEL]'."""
    cmd = ["gemini", "-p", prompt]
    if model:
        cmd += ["--model", model]
    return cmd, None


def _codex_cmd(
    prompt: str, model: Optional[str]
) -> Tuple[List[str], Optional[bytes]]:
    """OpenAI Codex CLI: `codex exec PROMPT [--model MODEL]'."""
    cmd = ["codex", "exec", prompt]
    if model:
        cmd += ["--model", model]
    return cmd, None


def _ollama_cmd(
    prompt: str, model: Optional[str]
) -> Tuple[List[str], Optional[bytes]]:
    """Ollama: `ollama run MODEL' (prompt via stdin).

    Different shape from the API-backed CLIs above. `ollama run' takes
    the model as a POSITIONAL argument and reads the prompt from
    stdin. The default model name (`llama3') is pulled here rather
    than in the Backend constructor so a missing `--model' on the
    role spec still produces a usable invocation.
    """
    cmd = ["ollama", "run", model or "llama3"]
    return cmd, prompt.encode("utf-8")


def builtin_backends() -> Dict[str, Backend]:
    """Fresh dict of the four built-in adapters; safe for the caller to mutate.

    We build a NEW dict on each call so a caller that wants to mutate
    a Backend's `default_model' (e.g., `config.build_registry' applying
    a TOML override) doesn't accidentally modify a shared instance
    across processes / tests.
    """
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
            # No default_model -- gemini's CLI evolves its own
            # default and we don't want to pin it.
            default_model=None,
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
    """Build a `Backend' from a config-defined command template.

    The template is a list of tokens; each token may contain
    `{prompt}' and/or `{model}' placeholders, expanded per call:

        ["my-tool", "--prompt={prompt}", "--model={model}"]

    Per-call expansion uses Python's `str.format', so doubled braces
    `{{' / `}}' are needed for literal braces. Unknown placeholders
    raise `BackendError' at call time (not at factory time -- the
    placeholder might be intentional but unsupported by `str.format').

    `prompt_via' chooses how the prompt reaches the subprocess:

        "argv"  -- `{prompt}' substitutes the prompt directly into
                   the argv. This is the common case.
        "stdin" -- the prompt is piped into the subprocess's stdin;
                   any `{prompt}' placeholder substitutes the empty
                   string (so users typically don't include
                   `{prompt}' in stdin-mode templates).

    Why expose both: some CLIs read prompts from stdin (ollama), some
    from argv (claude/gemini/codex). Defining the choice per-template
    lets users wire up either kind.

    The first token (post-substitution) is treated as the executable
    name for `run.check_dependencies'. We compute the binary by
    substituting empty values for `{prompt}' and `{model}' into the
    first token and taking the first whitespace-separated piece;
    this is best-effort and works for the typical case where the
    first token is just the binary name.

    Raises:
        BackendError -- empty `command_template', invalid `prompt_via',
                        or any other shape problem at factory time.
                        At call time, malformed placeholders in tokens
                        also raise BackendError.
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

    def build(
        prompt: str, model: Optional[str]
    ) -> Tuple[List[str], Optional[bytes]]:
        argv: List[str] = []
        # In stdin mode, `{prompt}' in argv tokens substitutes to
        # empty -- the actual prompt rides on stdin instead.
        prompt_value = prompt if prompt_via == "argv" else ""
        model_value = model or ""
        for tok in command_template:
            try:
                argv.append(
                    tok.format(prompt=prompt_value, model=model_value)
                )
            except (KeyError, IndexError) as e:
                # Unknown placeholder in the template (e.g. `{foo}').
                # We surface this as a BackendError so the calling
                # code in `run' logs it cleanly and the agent loop
                # can continue.
                raise BackendError(
                    f"backend {name!r}: bad placeholder in token {tok!r}: {e}"
                ) from e
        stdin = prompt.encode("utf-8") if prompt_via == "stdin" else None
        return argv, stdin

    # Best-effort binary inference: format the first token with empty
    # placeholders (so any `{prompt}' or `{model}' resolves to ""),
    # then split on whitespace and take the first piece. For the
    # typical `["my-tool", ...]' shape this gives `my-tool'.
    try:
        binary = command_template[0].format(prompt="", model="").split()[0]
    except (KeyError, IndexError, ValueError):
        # Pathological first token (placeholder we couldn't resolve,
        # empty after format, etc.). Fall back to the raw first
        # token; the dependency check will fail informatively.
        binary = command_template[0]

    return Backend(
        name=name,
        build_cmd=build,
        binary=binary,
        default_model=default_model,
        timeout=timeout,
    )


# -- role-spec parsing ---------------------------------------------------

# Role specs are the per-agent "which brain runs this role" notation
# the user types on the command line. Three forms:
#
#   `name'                     use the default backend + default model
#   `name@backend'             specific backend, its default model
#   `name@backend:model'       specific backend, specific model
#
# Names (role + backend) are alphanumeric plus `_' and `-', starting
# with a letter -- standard identifier shape. Models can contain `:'
# (think `provider/model:tag' style), `/', and `.', so the model tail
# is "anything but @" rather than a tighter character class.
#
# Anchored (`^...$') so partial matches don't slip through.
_ROLE_SPEC_RE = re.compile(
    r"^(?P<role>[A-Za-z][A-Za-z0-9_\-]*)"
    r"(?:@(?P<backend>[A-Za-z][A-Za-z0-9_\-]*)"
    r"(?::(?P<model>[^@]+))?)?$"
)


@dataclass
class RoleSpec:
    """One parsed role spec.

    Fields:
        role     The role name (left of any `@').
        backend  The backend name (between `@' and `:'), or None if
                 the spec was just `role' or `role:model' (the latter
                 is rejected by the regex; you can't specify a model
                 without a backend).
        model    The model string (right of `:'), or None.
    """
    role: str
    backend: Optional[str] = None
    model: Optional[str] = None


def parse_role_spec(spec: str) -> RoleSpec:
    """Parse `name' / `name@backend' / `name@backend:model'.

    Raises ValueError on a malformed spec.

    The error message intentionally lists the three valid forms so a
    user who typed `cook:claude-haiku' (wrong: missing backend) sees
    both what they did and what they should have done.
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
    """Resolve a parsed `RoleSpec' to a (Backend, model_override) pair.

    Backend selection rule
    ----------------------
    `spec.backend' if set, else `default_backend'. Unknown backend
    name -> ValueError listing the known backends (so a user who
    typo'd `gemnini' sees the real list).

    Model selection rules (in priority order)
    -----------------------------------------
    1. `spec.model' (the `:MODEL' tail of the spec) -- highest
       priority; the user said it explicitly.
    2. `cli_default_model' (the `--default-model' / config
       `defaults.model' value), but ONLY when `spec.backend' is None.
       Reason: the CLI's `--default-model' is conceptually tied to
       the DEFAULT backend; if the user explicitly picked a different
       backend with `@gemini', that backend's own default applies.
    3. None -- which `Backend.call()' resolves further by falling
       through to `Backend.default_model'.

    Why the `spec.backend is None' guard in rule 2: imagine the
    user runs

        chorale FILE cook ux@gemini --default-model claude-haiku-4-5

    Without the guard, "claude-haiku-4-5" would be passed to gemini,
    which doesn't know that model. With the guard, gemini gets its
    own default model and only `cook' gets the haiku override.
    """
    backend_name = spec.backend or default_backend
    if backend_name not in registry:
        raise ValueError(
            f"unknown backend {backend_name!r} for role {spec.role!r}; "
            f"known backends: {sorted(registry)}"
        )
    backend = registry[backend_name]

    # Apply the priority rules in order.
    if spec.model is not None:
        model_override: Optional[str] = spec.model
    elif spec.backend is None and cli_default_model is not None:
        model_override = cli_default_model
    else:
        model_override = None

    return backend, model_override
