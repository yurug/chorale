"""Prompt template for the agent loop.

Default prompt is tuned for the brainstorm-on-Markdown use case. Users
can override with --prompt-file PATH; the file content is read as a
Python `str.format`-style template with `{role}` and `{file_content}`
placeholders.
"""
from __future__ import annotations

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
    """Format a prompt template with the agent's role and current file."""
    return template.format(role=role, file_content=file_content)
