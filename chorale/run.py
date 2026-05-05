"""The agent runtime: cotype subprocess wrappers, per-agent loop, runner.

One thread per agent role. Each thread runs an independent open ->
claude -> splice -> save loop. The structural splice (in chorale.splice)
guarantees that two agents editing two different sections produce edits
in disjoint regions of the byte stream, so cotype's 3-way merge cannot
conflict between them.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from chorale.prompt import render_prompt
from chorale.splice import splice_section
from chorale.template import render_template


# A simple logger callable: log(role_or_None, message_str).
LogFn = Callable[[Optional[str], str], None]


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


def claude_complete(
    prompt: str, model: Optional[str], timeout: float = 60.0
) -> Optional[bytes]:
    """Call `claude --print -p PROMPT [--model MODEL]`. Bytes, or None on fail."""
    cmd = ["claude", "--print", "-p", prompt]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout if r.returncode == 0 else None


# -- agent loop ----------------------------------------------------------

def _agent_loop(
    role: str,
    idx: int,
    file: str,
    interval: float,
    stagger: float,
    model: Optional[str],
    prompt_template: str,
    stop: threading.Event,
    log: LogFn,
) -> None:
    """One role's polling loop, cooperatively cancellable via `stop`."""
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

        # Don't re-poll Claude for a base we already processed.
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
        agent_output = claude_complete(prompt, model)
        if agent_output is None:
            log(role, "claude failed or timed out")
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

class _Args:
    """Tiny config container; cli.py builds one of these."""
    interval: float
    stagger: float
    model: Optional[str]
    prompt_template: str


def check_dependencies() -> Optional[str]:
    """Return None if `cotype` and `claude` are on PATH, else an error string."""
    for tool in ("cotype", "claude"):
        if shutil.which(tool) is None:
            return f"{tool} not on PATH"
    return None


def run(file: Path, roles: List[str], args: _Args, log: LogFn) -> int:
    """Pre-allocate the template if needed, init cotype, spawn one thread
    per role, and block on KeyboardInterrupt. Returns process exit code."""
    if not file.exists() or file.stat().st_size == 0:
        file.write_bytes(render_template(roles))
        log(None, f"pre-allocated {file} with {len(roles)} agent sections")

    # cotype init is idempotent.
    _run("cotype", "init", str(file), "--json")

    # Refuse to start on an existing pending conflict; otherwise every
    # agent burns Claude calls on saves that can never succeed.
    if cotype_status(str(file)) == "conflicted":
        log(None,
            f"pending conflict on {file}; resolve it first with "
            f"`cotype resolve {file}` (after editing out markers).")
        return 5

    stop = threading.Event()
    threads = [
        threading.Thread(
            target=_agent_loop,
            args=(
                role, idx, str(file),
                args.interval, args.stagger, args.model,
                args.prompt_template, stop, log,
            ),
            daemon=True,
            name=f"chorale:{role}",
        )
        for idx, role in enumerate(roles)
    ]
    for t in threads:
        t.start()

    log(None, f"{len(roles)} agents on {file} (model={args.model or 'cli-default'}). Ctrl-C to stop.")
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
