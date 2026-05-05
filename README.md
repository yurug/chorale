# chorale

[![PyPI](https://img.shields.io/pypi/v/chorale.svg)](https://pypi.org/project/chorale/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> **Run N AI agents that brainstorm with you on a single text file, safely. Mix and match Claude, Gemini, Codex, Ollama, and your own.**

You write under `## user`. Each AI agent owns its own `## agent:<role>` section and edits it in place to reply. The file *is* the conversation — no chat windows, no scrolling transcripts, no lost edits. Concurrent saves are reconciled by [cotype](https://github.com/yurug/cotype)'s 3-way merge; the harness splices each agent's reply into ONLY its own section's bytes, so two agents editing two different sections **cannot conflict by construction**.

![chorale demo: three Claude personas + a note-taker brainstorming with a user in one shared file](https://raw.githubusercontent.com/yurug/cotype/main/examples/demo-crepe/demo.gif)

*Three Claude personas (cook, logistics, ux-designer) plus a note-taker design a school crêpe stand with the user — all in one `brainstorm.md`. Same demo applies to the multi-backend mode below; mix in `gemini`, `codex`, or `ollama` per role.*

```bash
pip install chorale

# all-claude (default)
chorale brainstorm.md cook logistics ux-designer note-taker

# mix four different brains in one chorale
chorale brainstorm.md \
    cook \
    logistics@gemini \
    ux-designer@codex:gpt-5 \
    note-taker@ollama:llama3
```

Edit `brainstorm.md` in any editor (with [`cotype-mode`](https://github.com/yurug/cotype/tree/main/editors/emacs) for live updates in Emacs); agents see your saves on their next poll and respond.

## Why this exists

Long sessions with AI agents drift into chat transcripts that scroll away from the work you actually want at the end. `chorale` flips it: the document accumulates in place, every actor has a labelled section, and disagreements between actors surface as inline diff3 markers rather than lost work.

The tool was extracted from `cotype`'s [`examples/headless-agents.sh`](https://github.com/yurug/cotype/blob/main/examples/headless-agents.sh) — that bash script is still the readable "what's the idea, on one screen" demo; this Python rewrite is the production-friendly version: tested, configurable, extensible.

## Install

```bash
pip install chorale
```

Requires Python ≥ 3.11, [`cotype`](https://pypi.org/project/cotype/) (auto-installed), and at least one supported AI CLI on PATH (`claude`, `gemini`, `codex`, `ollama`, or your own — see *Backends* below).

## Usage

```bash
chorale FILE ROLE_SPEC [ROLE_SPEC ...] [OPTIONS]
```

A **role spec** is one of:

| Form | Meaning |
|---|---|
| `cook` | default backend, default model |
| `cook@gemini` | gemini, gemini's default model |
| `cook@gemini:gemini-2.5-pro` | gemini, specific model |
| `cook@my-local` | a backend you defined in the config file |

Examples:

```bash
# four claude agents on a fresh brainstorm
chorale brainstorm.md cook logistics ux-designer note-taker

# mix brains: each role uses a different CLI
chorale brainstorm.md \
    cook \
    logistics@gemini \
    ux-designer@codex:gpt-5 \
    note-taker@ollama

# override the default backend for the whole run
chorale notes.md reviewer linter --default-backend gemini

# tighter polling, faster turns
chorale notes.md reviewer linter --interval 0.5 --stagger 2

# custom prompt template
chorale notes.md author editor --prompt-file my-prompt.txt
```

`chorale --help` prints the full surface, the role-spec syntax, the config-file format, and a copy-paste example.

### Backends

| Backend | Invocation | Default model |
|---|---|---|
| `claude` | `claude --print -p PROMPT --model MODEL` | `claude-sonnet-4-6` |
| `gemini` | `gemini -p PROMPT --model MODEL` | (gemini CLI's own default) |
| `codex` | `codex exec PROMPT --model MODEL` | (codex CLI's own default) |
| `ollama` | `ollama run MODEL` (prompt via stdin) | `llama3` |

You only need the binaries you actually use on PATH — a pure-`claude` run does not need `ollama` installed and vice versa.

### Config file

Optional. Default location: `~/.config/chorale/config.toml` (override with `--config PATH`).

```toml
[defaults]
backend = "claude"
model   = "claude-sonnet-4-6"

# Override a built-in's default model:
[backends.gemini]
default_model = "gemini-2.5-pro"

# Define a fully custom backend (e.g. a local model server, a research CLI):
[backends.my-local]
command       = ["my-tool", "--prompt={prompt}", "--model={model}"]
prompt_via    = "argv"        # or "stdin" to pipe the prompt instead
default_model = "v1"
timeout       = 90.0
```

A custom backend can then be referenced as `role@my-local` in any role spec.

### Custom prompts

Pass `--prompt-file PATH` to override the built-in brainstorm prompt. The file is treated as a `str.format` template with two placeholders the harness fills in per turn:

- `{role}` — the agent's role name (e.g. `cook`).
- `{file_content}` — the current state of the shared file.

Anything an agent emits *outside* its own `## agent:{role}` section is discarded by the splicer, so prompts only need to nudge the agent toward filling its own section sensibly.

### Stopping

`Ctrl-C` on the running process stops all agents cleanly. While running, you can edit the shared file in any editor; agents will see your edits on their next poll. If a conflict happens (you and an agent both edit the same section), `chorale` idles all agents and waits for you to resolve it (`cotype resolve FILE` after editing the markers).

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│  user (any editor) ─┐                                        │
│                     │ writes under ## user                   │
│                     ▼                                        │
│             ┌──── shared.md (cotype-managed) ────┐           │
│  agent_A ───┤                                    ├─── disk   │
│  agent_B ───┤   one section per actor            │           │
│  agent_C ───┘   diff3 reconciles concurrent saves│           │
│                 │                                │           │
│                 └────── chorale runtime ─────────┘           │
└─────────────────────────────────────────────────────────────┘
```

Each agent thread runs an independent loop:

1. `cotype status` — idle if a conflict is pending (only the user can resolve).
2. `cotype open` — capture a fresh base; skip if it hasn't changed since our last save.
3. `claude --print -p PROMPT` — generate a candidate reply.
4. **Splice**: parse the agent's output as Markdown sections, take *only* the body of `## agent:<role>`, splice it into the bytes from `base_path`. By construction, no other section's bytes can change.
5. `cotype save` — submit the spliced bytes; cotype decides direct / merged / noop / conflict.

The structural splice is the key idea: the agent can produce arbitrary content, but only its own section's bytes ever reach the file. Two agents editing two different sections produce edits in disjoint byte ranges, no matter how adjacent the section *headers* are.

## Tests

```bash
pip install pytest
pytest -q
```

Tests cover the splicer's contract (round-trip, role isolation, codefence stripping, no-change short-circuit) and the template generator. The runtime (subprocess wrappers, threading) is intentionally untested — it's almost entirely IO and best validated by running the demo.

## Compared to

- **[cotype](https://github.com/yurug/cotype)** — the byte-level safe-save CLI underneath. `chorale` is the agent harness; `cotype` is the merge engine.
- **[`headless-agents.sh`](https://github.com/yurug/cotype/blob/main/examples/headless-agents.sh)** — the original bash version, still in cotype's repo as a one-screen reference. `chorale` is the same idea with structure (config, tests, prompt extension point).

## License

MIT. See [LICENSE](LICENSE).
