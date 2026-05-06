# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] — 2026-05-06

CLI-only patch release; documentation-only.

- **Literate-programming pass over `chorale/`.** No behavioural change,
  no API change. Module-level docstrings, function docstrings, and
  inline comments rewritten so the source itself is now a pedagogical
  introduction to chorale's design: the section-aware splice as the
  conflict-killer, the per-role placeholder anchors that diff3 needs,
  the why-CLI-not-SDK choice, the role-spec resolution rules, the
  threaded agent runtime, and the layered error handling in the
  dispatcher are all explained in-source. Reading the package now
  teaches the model.

## [0.2.0] — 2026-05-05

Multi-backend support. Different roles in the same chorale can now use
different AI brains; built-in adapters for claude, gemini, codex, and
ollama; custom backends via a TOML config file.

### Added

- **Role-spec syntax** for selecting a backend (and optionally a model)
  per role:
  - `cook` — default backend, default model.
  - `cook@gemini` — gemini, gemini's default model.
  - `cook@gemini:gemini-2.5-pro` — gemini, specific model.
- **Built-in backends**: `claude` (default), `gemini`, `codex`, `ollama`.
  Each is a tiny adapter that knows the right CLI invocation for that
  tool (argv vs stdin, model flag vs positional, etc.).
- **TOML config file** at `~/.config/chorale/config.toml` (override with
  `--config PATH`). Override the default backend, the default model, or
  any built-in's default model; define fully custom backends with a
  `command = [...]` template using `{prompt}` and `{model}` placeholders.
- **`--default-backend NAME`** and **`--default-model NAME`** CLI flags
  to override the config file's defaults.
- **Per-backend dependency check**: only the binaries actually used by
  some role need to be on PATH. Mixing `claude` + `ollama` requires
  both, but a pure-claude run no longer asks for `ollama`.
- **Per-agent visibility**: the startup banner now shows each agent's
  role + backend + model, so you can verify the wiring at a glance.
- 40 new tests covering role-spec parsing, each built-in's argv shape,
  custom-backend factory (argv mode + stdin mode), and TOML config
  loading.

### Changed

- The `--model` flag is renamed to `--default-model` (with `--model`
  kept as an alias for backwards compatibility with 0.1.0 invocations).

[Unreleased]: https://github.com/yurug/chorale/compare/v0.2.1...HEAD
[0.2.1]:      https://github.com/yurug/chorale/releases/tag/v0.2.1
[0.2.0]:      https://github.com/yurug/chorale/releases/tag/v0.2.0
[0.1.0]:      https://github.com/yurug/chorale/releases/tag/v0.1.0

First public release. Production-friendly Python rewrite of the
`headless-agents.sh` recipe that lives in
[cotype](https://github.com/yurug/cotype/blob/main/examples/headless-agents.sh).

### Added

- `chorale FILE ROLE...` console script: spawn one Claude agent per role,
  each owning its own `## agent:<role>` section. Saves go through
  `cotype` so concurrent edits never lose work.
- **Section-aware splice**: agent outputs are parsed as Markdown sections;
  only the body of `## agent:<role>` is extracted and spliced into the
  base bytes. Two agents editing two different sections cannot conflict
  by construction.
- **Pre-allocated template**: `## user` plus one `## agent:<role>` per
  agent, each with a unique placeholder body, written when the file is
  empty. Gives diff3 stable per-section anchors.
- **Configurable**:
  - `--interval` / `--stagger` (polling cadence and per-agent startup phase).
  - `--model` (default `claude-sonnet-4-6`; pass empty for CLI default).
  - `--prompt-file` (override the built-in brainstorm prompt; `{role}`
    and `{file_content}` placeholders).
- **Conflict-aware idle**: agents detect a pending cotype conflict and
  stop polling Claude until the user resolves; resume automatically.
- **Skip-on-no-change**: if Claude returns the same section body that's
  already in the file, no save is attempted -- avoids the
  noop-with-whitespace-drift race.
- 12 unit tests covering the splicer's contract and the template generator.

### Known limitations

- Markdown sections only; non-Markdown formats (JSON, code) would need
  per-format splicers. Those are a natural future addition.
- No persistent agent memory between turns; each Claude call sees only
  the current file. Add it via `--prompt-file` if your use case needs it.
- Agents run as threads (one Python process). For very large agent
  counts or genuinely independent processes, fork the agent loop into
  separate processes -- not done yet because no use case has needed it.

