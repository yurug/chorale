"""Prompt template for the agent loop.

What this file defines
======================

Two things:

  - `DEFAULT_PROMPT' -- a `str.format'-style template, applied to
    every Claude/Gemini/etc. call when the user hasn't passed
    `--prompt-file'. It's tuned for the brainstorm-on-Markdown use
    case shown in chorale's README.

  - `render_prompt(template, role, file_content)' -- a one-liner
    that fills in `{role}' and `{file_content}' placeholders. Custom
    prompts (via --prompt-file) MUST use those two placeholders and
    no others.

What the default prompt actually says
=====================================

A glance at `DEFAULT_PROMPT' makes the wire-contract with the LLM
explicit. Highlights:

  - Each participant owns ONE section. The model is told its section
    is `## agent:{role}'.
  - When it has nothing new to say, leave the section unchanged.
    This combined with the splicer (which detects "no semantic
    change" and returns None) means the LLM-with-nothing-to-say does
    not produce a save and does not waste a cycle.
  - Be terse. 80 words max, 2-3 sentences. Long replies block the
    conversation -- in a 4-agent chorale, a 500-word monologue from
    one agent stalls everyone else's read window.
  - Output the entire file. We don't strictly NEED that (the splicer
    only looks at the agent's own section), but asking for the whole
    file is the simplest format and lets the LLM see what it's
    contributing to.
  - "Anything you write outside `## agent:{role}`'s body will be
    discarded." This is true: the splicer is the enforcement. Saying
    so explicitly stops the LLM from trying too hard on other
    sections, which has been observed to produce more focused replies.

Why a `str.format'-style template, not Jinja or similar
=======================================================

We need exactly two replacements: `{role}' and `{file_content}'.
Anything more sophisticated would invite users to put loops or
conditionals in their prompt files, which is a power most prompts
don't actually need. `str.format' is in the stdlib, has known
performance, and the syntax is universally understood. The cost of
the choice is that literal `{` / `}` in a custom prompt must be
doubled (`{{' / `}}') -- the same gotcha as in any `str.format'
template, and a small ask of authors who only ever write two-
placeholder templates anyway.
"""
from __future__ import annotations

# The default prompt, tuned for brainstorm-on-Markdown. Bullet by
# bullet: each line is making a specific deal with the LLM. Keep an
# eye on the explicit "Be terse" line in particular -- it's load-
# bearing for the chained-conversation feel of the demo.
DEFAULT_PROMPT = """\
You are agent:{role} collaborating with a human user and other agents on a shared Markdown file. Each participant owns exactly ONE section: `## user`, `## agent:{role}`, and one `## agent:<other>` per other agent.

Your section is `## agent:{role}`. Its body initially contains a placeholder line; when you have something to say AS {role}, replace the placeholder (or your previous reply) with your new reply.

Be terse. Aim for 2-3 short sentences or a small bullet list, well under 80 words. The other agents and the user are watching the file in real time -- a long monologue blocks the conversation.

You can ignore the contents of the OTHER `## agent:<other>` sections in your output -- the harness will splice ONLY your own section's body back into the file. Anything you write outside `## agent:{role}`'s body will be discarded.

If neither the user nor another agent has asked for your input AS {role} since your last reply, leave `## agent:{role}`'s body unchanged.

Output the entire file (it's the simplest format), no preamble, no code fences, no commentary.

<file>
{file_content}
</file>"""


def render_prompt(template: str, role: str, file_content: str) -> str:
    """Format `template' with the agent's role and the current file.

    The two placeholders `{role}' and `{file_content}' are
    substituted; any other `{...}' in the template either resolves
    against another keyword arg (raising KeyError -- which the caller
    in run.py converts to a logged failure rather than a crash) or
    survives literally if it was a doubled `{{' / `}}'.

    Pure: doesn't read or write anywhere; just calls `str.format'.
    """
    return template.format(role=role, file_content=file_content)
