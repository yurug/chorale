"""chorale -- run N AI agents collaborating on a single text file via cotype.

What this package is
====================

A small Python tool that drives multiple AI agents (each backed by a
different CLI: Claude, Gemini, Codex, Ollama, or anything you wire up
via a TOML config) so they collaborate WITH the user on a shared
Markdown file. The file is the workspace; the agents own labelled
`## agent:<role>' sections; the user owns `## user'. Concurrent saves
are reconciled by `cotype's 3-way merge.

The interesting trick: chorale does NOT trust an LLM's full-file
output byte-for-byte. After each Claude/Gemini/etc. call, the harness
parses out only the body of the agent's own `## agent:<role>` section
and splices it into the bytes from cotype's `base_path'. Everything
outside the agent's section therefore comes from the base verbatim.
Two agents editing two different sections produce edits in
**disjoint byte ranges** -- they cannot conflict by construction.

Mental model
============

  while not stopped:
      1. cotype status FILE        -- if conflicted, idle.
      2. cotype open FILE          -- capture base_sha + base_path.
      3. read base_path            -- the actual current bytes.
      4. backend.call(prompt, model)
                                   -- subprocess: claude/gemini/etc.
      5. splice_section(base, output, role)
                                   -- take only the agent's own
                                      section's body; build new bytes
                                      that differ from base ONLY in
                                      that section.
      6. cotype save FILE          -- atomic publish; cotype merges
                                      against any concurrent write.

Step 5 is the conflict-killer. Without it we'd be relying on the LLM
to faithfully reproduce every byte of the file outside its own
section -- which they don't, even when explicitly told to. With it,
the LLM can produce arbitrary garbage outside its section and the
file remains correct.

Module map (read in this order if you want the whole story)
===========================================================

  splice.py     The conflict-killer: pure-functional Markdown section
                splice. `splice_section(base, agent_output, role)'
                returns either new bytes (with only the agent's
                section body changed) or None (no semantic change).

  template.py   Pre-allocated section template: `## user' + one
                `## agent:<role>' per agent, each with a unique
                placeholder line so diff3 has stable per-section
                anchors.

  prompt.py     The default brainstorm prompt + a `render_prompt'
                helper. Custom prompts via --prompt-file PATH.

  backends.py   Adapter layer for CLI backends. Built-ins for claude
                / gemini / codex / ollama, factory for custom ones,
                role-spec parser, and the resolution rules that turn
                `cook@gemini:gemini-2.5-pro' into a (Backend, model)
                pair.

  config.py     TOML config loader + registry assembler. Lets users
                override defaults, tune per-built-in defaults, or
                define wholly custom backends.

  run.py        The runtime: cotype subprocess wrappers, threaded
                per-agent loop, the runner that glues it all together.

  cli.py        argparse + dispatch. Thin layer; the heavy lifting
                lives in run.py and friends.

  __main__.py   Allows `python -m chorale ...' alongside the installed
                `chorale' console script.

Dependencies
============

Python >= 3.11 (uses `tomllib' for config parsing) plus:

  - `cotype'  on PATH (auto-installed as a pip dependency).
  - At least one of `claude', `gemini', `codex', `ollama' on PATH --
    only the binaries actually used by some role need to exist; the
    `--help' is happy to render with none of them present.

Why not API SDKs instead of CLI subprocess?
===========================================

We deliberately go through each provider's first-party CLI rather
than calling their HTTP APIs directly. Reasons:

  - Authentication is the user's problem, not chorale's. They've
    already configured `claude auth' or `gemini auth' or whatever;
    we inherit that.
  - Each CLI's prompt-shaping decisions (system prompt, model
    selection, rate-limit handling, JSON mode) are theirs to evolve.
    Chorale stays format-agnostic.
  - Adding a new provider becomes one new entry in `BUILTIN_BACKENDS'
    or one new TOML section, not a new SDK dependency.

The cost is the per-call subprocess hop (~0.5-1 s for API-backed
CLIs; negligible for `ollama'). For a multi-agent brainstorm where
each turn is several seconds of inference anyway, that's a rounding
error.
"""
__version__ = "0.2.1"
