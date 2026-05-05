"""The agent runtime: cotype subprocess wrappers, per-agent loop, runner.

One thread per agent. Each thread runs an independent open ->
backend.call -> splice -> cotype save loop. The structural splice
(in chorale.splice) guarantees that two agents editing two different
sections produce edits in disjoint regions of the byte stream, so
cotype's 3-way merge cannot conflict between them.

Each agent has its own `Backend` (claude / gemini / codex / ollama /
custom) and optionally a model override -- so different roles in the
same chorale can use different brains.
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


# A simple logger: log(role_or_None, message_str).
LogFn = Callable[[Optional[str], str], None]


@dataclass
class AgentConfig:
    """One agent's resolved configuration."""

    role: str
    backend: Backend
    model: Optional[str]


# -- cotype subprocess helpers -------------------------------------------

def _run(*args: str, input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), input=input, capture_output=True, check=False
    )


def cotype_status(file: str) -> str:
    """Return 'unmanaged' / 'clean' / 'conflicted'; '??' on parse failure."""
    r = _run("cotype", "status", file, "--json")
    if r.returncode != 0:
        return "??"
    try:
        return json.loads(r.stdout).get("status", "??")
    except json.JSONDecodeError:
        return "??"


def cotype_open(file: str) -> Optional[dict]:
    """Capture a fresh base; return parsed payload or None on error."""
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
    """Run `cotype save` with `content` on stdin. Returns parsed JSON."""
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
    """One agent's polling loop, cooperatively cancellable via `stop`."""
    role = agent.role
    backend = agent.backend
    model = agent.model

    # Stagger startup so agent N typically sees agent N-1's reply on its
    # first open, producing a chained conversation rather than parallel
    # monologues at the same base.
    if stop.wait(idx * stagger):
        return

    last_sha: Optional[str] = None

    while not stop.is_set():
        # Idle while a conflict is pending; only the user can resolve.
        if cotype_status(file) == "conflicted":
            if stop.wait(interval):
                return
            continue

        meta = cotype_open(file)
        if meta is None:
            if stop.wait(interval):
                return
            continue

        base_sha = meta.get("base_sha")
        base_path = meta.get("base_path")
        if not (base_sha and base_path):
            if stop.wait(interval):
                return
            continue

        if base_sha == last_sha:
            if stop.wait(interval):
                return
            continue

        try:
            base_bytes = Path(base_path).read_bytes()
            file_content = base_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log(role, f"could not read base_path: {e}")
            if stop.wait(interval):
                return
            continue

        prompt = render_prompt(prompt_template, role, file_content)
        agent_output = backend.call(prompt, model)
        if agent_output is None:
            log(role, f"{backend.name} failed or timed out")
            if stop.wait(interval):
                return
            continue
        if not agent_output.strip():
            log(role, "empty response, skipped")
            last_sha = base_sha
            if stop.wait(interval):
                return
            continue

        spliced = splice_section(base_bytes, agent_output, role)
        if spliced is None:
            log(role, "no change in own section, skipped")
            last_sha = base_sha
            if stop.wait(interval):
                return
            continue

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
    """Tiny config container; cli.py builds one of these."""

    interval: float
    stagger: float
    prompt_template: str


def check_dependencies(agents: List[AgentConfig]) -> Optional[str]:
    """Return None if `cotype` plus every agent's backend binary is on
    PATH, else an error string with the missing tool's name."""
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
    """Pre-allocate the template if needed, init cotype, spawn one
    thread per agent, and block on KeyboardInterrupt. Returns an exit
    code."""
    roles = [a.role for a in agents]
    if not file.exists() or file.stat().st_size == 0:
        file.write_bytes(render_template(roles))
        log(None, f"pre-allocated {file} with {len(roles)} agent sections")

    # cotype init is idempotent.
    _run("cotype", "init", str(file), "--json")

    # Refuse to start on an existing pending conflict.
    if cotype_status(str(file)) == "conflicted":
        log(None,
            f"pending conflict on {file}; resolve it first with "
            f"`cotype resolve {file}` (after editing out markers).")
        return 5

    # Summary line so the user sees who's using what brain.
    fleet = ", ".join(
        f"{a.role}@{a.backend.name}"
        + (f":{a.model}" if a.model else "")
        for a in agents
    )
    log(None,
        f"{len(agents)} agents on {file} -- {fleet}. Ctrl-C to stop.")

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

    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        log(None, "stopping...")
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
    return 0
