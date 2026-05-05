# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-05

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

[Unreleased]: https://github.com/yurug/chorale/compare/v0.1.0...HEAD
[0.1.0]:      https://github.com/yurug/chorale/releases/tag/v0.1.0
