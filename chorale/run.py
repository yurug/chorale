"""The agent runtime: cotype subprocess wrappers, per-agent loop, runner.

Where this module sits
======================

Above this module: `cli.py' parses argv into a list of `AgentConfig'
plus a `RunArgs' bundle, then calls `run()'.

Below this module: each `cotype_*' helper shells out to the `cotype'
binary; each agent's per-iteration `Backend.call(...)' shells out to
`claude' / `gemini' / etc.; the splice + template + prompt modules
provide pure helpers.

`run.py' itself is the orchestration: spawn one thread per role,
each thread runs the open -> backend -> splice -> save loop, the
main thread waits for either Ctrl-C or every thread to die.

Process model
=============

ONE Python process. ONE thread per agent. Each agent thread runs an
independent loop, and the threads do NOT share state with each
other (no locks, no queues, nothing) -- they coordinate purely
through the file on disk via cotype's safe-save protocol.

The choice of threads (vs. processes / asyncio) is mostly about
simplicity: the work each thread does is dominated by subprocess
calls, so the GIL is released for >99% of wall time. Asyncio would
be a more "Pythonic" structure but doesn't pay rent here -- the
thread-per-agent model is easier to reason about and easier to
Ctrl-C cleanly.

Lifecycle of one iteration
==========================

For each tick (every `args.interval' seconds):

  1. cotype status FILE -- if the file is in a pending-conflict
     state, idle. The user has to clear it (`cotype resolve') before
     any agent can save again.

  2. cotype open FILE -- capture a base. Returns base_sha + base_path.
     If we've already seen this base_sha (last_sha), nothing new
     has happened since our last cycle; sleep and try again.

  3. Read base_path off disk -- THIS is the bytes the agent will
     edit. Reading FILE directly would be racy (per cotype's
     documented "forbidden protocol"); base_path is pinned bytes.

  4. backend.call(prompt, model) -- subprocess call to the agent's
     CLI. Returns the agent's full-file output (or None on failure).

  5. splice_section(base_bytes, agent_output, role) -- the
     conflict-killer. Take only the agent's own section's body from
     its output, splice into base_bytes. Returns None if nothing
     changed.

  6. cotype save FILE -- atomic publish. cotype handles 3-way merge
     against any concurrent write that landed during the LLM call.
     Possible outcomes: direct, merged, noop, conflict, error.

  7. Sleep until the next tick.

Stagger
=======

Agent thread N waits `idx * stagger' seconds before its first poll.
With 4 agents at the default 3s stagger, the timeline of first
polls is:

    t=0s: cook polls
    t=3s: logistics polls (sees cook's reply if it landed)
    t=6s: ux-designer polls (sees both)
    t=9s: note-taker polls (sees all three)

This produces a "chained conversation" feel rather than four
agents shouting at the same base simultaneously. After the first
round, cycles drift naturally into less-synchronised tempos as
each agent's LLM calls take varying time.

Error handling philosophy
=========================

Agent loops are RESILIENT, not eager-to-stop. Almost every failure
mode -- subprocess timeouts, garbage JSON from cotype, an LLM that
took an unusual prompt path, file-system blips -- is logged once
and the loop continues to the next tick. The exceptions are:

  - `stop' (a `threading.Event') is set, meaning Ctrl-C from the
    main thread. The loop returns immediately.
  - The cotype binary itself isn't on PATH (caught upfront in
    `check_dependencies'). The loop never starts.

The "log and continue" stance means a misbehaving backend doesn't
take down the whole chorale -- if `gemini' is down, the role
that uses it goes quiet, and the rest of the chorale keeps working.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from chorale.backends import Backend
from chorale.prompt import render_prompt
from chorale.splice import splice_section
from chorale.template import render_template


# Logger callable: log(role_or_None, message_str). Logs from the main
# thread pass `None' for `role' (and get the `[chorale]' prefix);
# logs from an agent thread pass the role name.
LogFn = Callable[[Optional[str], str], None]


@dataclass
class AgentConfig:
    """One agent's resolved configuration -- the output of role-spec resolution.

    Built by `cli._build_agents' from the user's role-spec strings,
    consumed by `run.run()' to spawn threads.

    Fields:
        role     The role name (left of any `@' in the original spec).
                 Becomes the `## agent:<role>' section in the file
                 and appears in log lines.
        backend  The chosen `Backend' object (built-in or custom).
        model    Optional per-role model override. None means "use
                 backend.default_model".
    """

    role: str
    backend: Backend
    model: Optional[str]


# -- cotype subprocess helpers -------------------------------------------

def _run(*args: str, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    """Tiny wrapper around `subprocess.run' for the cotype calls below."""
    return subprocess.run(
        list(args), input=input, capture_output=True, check=False
    )


def cotype_status(file: str) -> str:
    """Return 'unmanaged' / 'clean' / 'conflicted'; '??' on parse failure.

    The `??' fallback covers two cases: cotype exits non-zero
    (unlikely; status is read-only and almost always succeeds) and
    cotype output isn't JSON (shouldn't happen with `--json').
    Either case is logged-and-continue territory rather than a
    hard error.
    """
    r = _run("cotype", "status", file, "--json")
    if r.returncode != 0:
        return "??"
    try:
        return json.loads(r.stdout).get("status", "??")
    except json.JSONDecodeError:
        return "??"


def cotype_open(file: str) -> Optional[dict]:
    """Capture a fresh base; return parsed payload or None on error.

    Returns the full JSON envelope from `cotype open', not just
    `base_sha' -- the caller (`_agent_loop') wants `base_path' too,
    and may want `conflicted' / `pending_conflict' fields in future.
    """
    r = _run("cotype", "open", file, "--json")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def cotype_save(
    file: str, base_sha: str, actor: str, content: bytes
) -> Optional[dict]:
    """Run `cotype save' with `content' on stdin. Returns parsed JSON.

    The `--actor' label is a free-form string cotype records in
    conflict metadata. Chorale uses `agent:<role>' so a forensic
    look at `<sidecar>/conflicts/<id>/meta.json' tells you which
    actor produced the conflict.
    """
    r = _run(
        "cotype", "save", file,
        "--base-sha", base_sha, "--actor", actor, "--json",
        input=content,
    )
    try:
        return json.loads(r.stdout) if r.stdout else None
    except json.JSONDecodeError:
        return None


# -- agent loop ----------------------------------------------------------

def _agent_loop(
    agent: AgentConfig,
    idx: int,
    file: str,
    interval: float,
    stagger: float,
    prompt_template: str,
    stop: threading.Event,
    log: LogFn,
) -> None:
    """One agent's polling loop, cooperatively cancellable via `stop'.

    See module docstring for the lifecycle. Important details:

      - `stop.wait(N)' is the cancellable sleep: returns True if
        `stop' is set during the wait (meaning we should bail), False
        if the timeout expired normally. Used at every "sleep until
        next tick" point.

      - `last_sha' is per-thread: each agent tracks the most recent
        base_sha it processed. If `cotype open' returns the same
        base_sha as last cycle, nothing has changed since we last
        looked; we skip the LLM call (which is the expensive part)
        and just sleep.

      - The "splice -> None" path is critical. When the LLM returns
        a response that doesn't actually change the agent's section
        (it had nothing new to say, or only whitespace-tweaked
        outside its section), the splicer returns None and we skip
        the `cotype save' call entirely. This kills the noop-with-
        drift race that would otherwise happen when one agent saves
        a no-op-equivalent that just barely differs in whitespace
        from the on-disk bytes.
    """
    role = agent.role
    backend = agent.backend
    model = agent.model

    # Stagger startup: agent N (0-indexed) waits N * stagger seconds
    # before its first poll. See module docstring.
    if stop.wait(idx * stagger):
        return

    last_sha: Optional[str] = None

    while not stop.is_set():
        # 1. Idle while a conflict is pending. Only the user can
        # resolve it (via `cotype resolve' after editing markers
        # out); no save will succeed until then. Sleep and re-check
        # next cycle -- when the user clears the conflict, the
        # status flips back to "clean" and we resume.
        if cotype_status(file) == "conflicted":
            if stop.wait(interval):
                return
            continue

        # 2. Capture a fresh base.
        meta = cotype_open(file)
        if meta is None:
            # cotype open failed (file gone? sidecar corrupt?). We
            # don't crash; we sleep and try again. If this is
            # persistent, the user will notice the lack of activity.
            if stop.wait(interval):
                return
            continue

        base_sha = meta.get("base_sha")
        base_path = meta.get("base_path")
        if not (base_sha and base_path):
            # Malformed cotype response. Same treatment as above:
            # log nothing, sleep, retry.
            if stop.wait(interval):
                return
            continue

        # 3. Skip cycle when the base hasn't changed since our last
        # successful round. This is the "watch-and-wait" optimisation:
        # if no agent (and no user) has saved anything since we last
        # opened, there's nothing for THIS agent to react to. Sleep.
        if base_sha == last_sha:
            if stop.wait(interval):
                return
            continue

        # 4. Read base_path -- the actual bytes the agent will edit
        # against. Per cotype's protocol, we MUST read from base_path
        # (not from FILE directly), to avoid the race where another
        # writer lands between `cotype open' and our read.
        try:
            base_bytes = Path(base_path).read_bytes()
            file_content = base_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log(role, f"could not read base_path: {e}")
            if stop.wait(interval):
                return
            continue

        # 5. Render the prompt and call the backend. The backend
        # handles its own timeout / failure cases internally and
        # returns None on any of them; we treat None uniformly as
        # "this round produced no useful output, sleep and retry".
        prompt = render_prompt(prompt_template, role, file_content)
        agent_output = backend.call(prompt, model)
        if agent_output is None:
            log(role, f"{backend.name} failed or timed out")
            if stop.wait(interval):
                return
            continue
        if not agent_output.strip():
            # The LLM returned nothing usable. We update last_sha so
            # we don't immediately retry against the same base
            # next cycle.
            log(role, "empty response, skipped")
            last_sha = base_sha
            if stop.wait(interval):
                return
            continue

        # 6. The conflict-killer: extract only the agent's own section
        # body from `agent_output' and splice it into `base_bytes'.
        # If the splicer returns None, the agent has nothing new for
        # its section (either it dropped the section, or the body it
        # produced is byte-equal to base's body). Skip the save.
        spliced = splice_section(base_bytes, agent_output, role)
        if spliced is None:
            log(role, "no change in own section, skipped")
            last_sha = base_sha
            if stop.wait(interval):
                return
            continue

        # 7. Save through cotype. The `--actor agent:<role>' label is
        # what shows up in conflict meta.json if a conflict happens
        # despite the splicer's guarantee (which would only happen if
        # the user is editing the SAME section as the agent at the
        # same time -- a genuine conflict, not a spurious one).
        result = cotype_save(file, base_sha, f"agent:{role}", spliced)
        if result is None:
            log(role, "save: no JSON response from cotype")
        else:
            status = result.get("status", "??")
            if status == "saved":
                log(role, f"save: {result.get('mode', '?')}")
            elif status == "conflict":
                cid = (result.get("conflict_id") or "")[:8]
                log(role, f"conflict {cid} -- markers written to {file}")
            elif status == "error":
                log(role,
                    f"error: {result.get('error')} -- {result.get('message')}")

        last_sha = base_sha
        if stop.wait(interval):
            return


# -- runner --------------------------------------------------------------

@dataclass
class RunArgs:
    """Tiny config container; cli.py builds one of these.

    Kept minimal -- chorale's tunable parameters are deliberately
    few. Adding more belongs in `Backend' (per-backend) or in the
    role spec (per-role), not here.
    """

    interval: float
    stagger: float
    prompt_template: str


def check_dependencies(agents: List[AgentConfig]) -> Optional[str]:
    """Return None if all required CLIs are on PATH, else an error string.

    "Required" means: `cotype' always, plus the `binary' field of
    every distinct backend that some role uses. A pure-claude run
    requires `cotype' and `claude'; mixing in `ollama' for one role
    additionally requires `ollama'. A backend whose binary appears
    multiple times (because three roles all use it) is checked once.

    Returning a string instead of raising lets the calling code
    (`cli.main') handle the no-CLI case as a usage error rather
    than a crash.
    """
    if shutil.which("cotype") is None:
        return "cotype not on PATH (try: pip install cotype)"
    seen = set()
    for a in agents:
        b = a.backend.binary
        if b in seen:
            continue
        seen.add(b)
        if shutil.which(b) is None:
            return (
                f"{b} not on PATH (required for backend {a.backend.name!r}, "
                f"used by role {a.role!r})"
            )
    return None


def run(file: Path, agents: List[AgentConfig], args: RunArgs, log: LogFn) -> int:
    """Pre-allocate template + spawn one thread per agent + block on Ctrl-C.

    Returns a process exit code:
      0 -- normal termination (Ctrl-C, or all threads exited).
      5 -- the file was already in a pending-conflict state at
           startup; we refuse to start because no save can succeed.

    Steps (in order):

      1. If FILE is empty (or doesn't exist), seed it with the
         pre-allocated section template (`render_template'). This
         is what gives diff3 the per-section anchors it needs to
         not spuriously conflict on adjacent edits.

      2. `cotype init' the file (idempotent if already managed).

      3. Refuse to start if a conflict is already pending; better
         to fail fast and tell the user to resolve than to spawn N
         agents that will all immediately hit `ConflictPending'.

      4. Log a summary "fleet" line so the user can verify the
         resolved (role, backend, model) tuples at a glance.

      5. Spawn one daemon thread per agent. Daemons because we want
         them to die with the process; the explicit `stop.set()' on
         Ctrl-C is the orderly path, the daemon flag is the
         backstop.

      6. Block on KeyboardInterrupt. The polling join loop is so
         that Python's signal handler runs with reasonable latency
         (a long single `t.join()' would block the main thread
         from observing Ctrl-C until the thread happened to exit).
    """
    roles = [a.role for a in agents]
    if not file.exists() or file.stat().st_size == 0:
        file.write_bytes(render_template(roles))
        log(None, f"pre-allocated {file} with {len(roles)} agent sections")

    # cotype init is idempotent on an already-managed file. We run it
    # unconditionally so the sidecar exists by the time the agents
    # start polling.
    _run("cotype", "init", str(file), "--json")

    # Refuse to start on an existing pending conflict. Otherwise
    # every agent burns LLM cycles on saves that will be rejected.
    if cotype_status(str(file)) == "conflicted":
        log(None,
            f"pending conflict on {file}; resolve it first with "
            f"`cotype resolve {file}` (after editing out markers).")
        return 5

    # Summary line: who's using what brain. Reads like
    # "cook@claude:claude-sonnet-4-6, logistics@gemini, ...".
    fleet = ", ".join(
        f"{a.role}@{a.backend.name}"
        + (f":{a.model}" if a.model else "")
        for a in agents
    )
    log(None,
        f"{len(agents)} agents on {file} -- {fleet}. Ctrl-C to stop.")

    # Spawn agent threads. `daemon=True' means the threads die with
    # the main thread; we still ALSO use `stop' for an orderly
    # shutdown so each thread's last action is a clean exit (no
    # half-written cotype save left hanging).
    stop = threading.Event()
    threads = [
        threading.Thread(
            target=_agent_loop,
            args=(
                agent, idx, str(file),
                args.interval, args.stagger,
                args.prompt_template, stop, log,
            ),
            daemon=True,
            name=f"chorale:{agent.role}",
        )
        for idx, agent in enumerate(agents)
    ]
    for t in threads:
        t.start()

    # Main-thread wait. The poll-join loop with a small timeout lets
    # Python's signal handler (which delivers KeyboardInterrupt)
    # run promptly when the user hits Ctrl-C, instead of being
    # stuck inside a single `t.join()' call.
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        # Orderly shutdown: signal the threads to stop, then give
        # them a couple of seconds to wrap up. Daemon flag handles
        # any that overrun.
        log(None, "stopping...")
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
    return 0
