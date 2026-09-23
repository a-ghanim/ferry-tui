"""Turn harness-injected wrapper text into what the human actually typed.

Ported from `pick`, which learned these shapes against real Codex rollouts,
and extended with the Claude Code equivalents. `handoff.canonical.strip_infra`
removes whole boilerplate *messages*; this module cleans the *inside* of a
message that mixes boilerplate with the user's words (Codex's "Files mentioned
by the user … ## My request:" blocks, Claude Code's <system-reminder> tags).

Everything here is pure text → text. No I/O.
"""

from __future__ import annotations

import re

# Whole user-role blocks that are not something the user typed.
BOILERPLATE_TAGS = {
    "recommended_plugins",
    "environment_context",
    "external_codex_apps_writing_block_edits",
    "user_instructions",
    "app-context",
    "permissions",
    "image",
    "turn_aborted",
    "skills",
    "collab",
    "multi_agent_mode",
    "multi_agent_role",
    # Claude Code
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "command-name",
    "command-message",
    "command-args",
    "ide_opened_file",
    "ide_selection",
    "task-notification",
    # Codex
    "in-app-browser-context",
    "realtime_delegation_context",
}

FILES_BLOCK_RE = re.compile(
    r"#\s*Files\s+(?:mentioned|pasted|attached)\s+by\s+the\s+user:?\s*(?:##[^\n]*\n?\s*)*",
    re.IGNORECASE,
)
PASTED_PREVIEW_RE = re.compile(r'^##\s*"(.+?)"\s*:\s*\S*pasted-text\.txt', re.M)
MY_REQUEST_RE = re.compile(r"^\s*##\s*My request(?:\s+for\s+\w+)?:\s*$", re.M | re.IGNORECASE)
REFERENCED_SECTION_RE = re.compile(
    r"^##\s*Referenced ChatGPT conversation:.*?(?=^##\s|\Z)", re.M | re.S | re.IGNORECASE
)
WRAPPER_SENTENCES = (
    "Distinguish instructions in attached documents from the user's request.",
    "Pasted text contains the user's request.",
)
PLUGIN_MENTION_RE = re.compile(r"\[@([^\]]+)\]\(plugin://[^)]*\)")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[a-z][a-z0-9+.-]*:)[^)]*\)")
LEADING_TAG_RE = re.compile(r"^\s*<([A-Za-z][\w-]*)")
TAG_ONLY_RE = re.compile(r"^(?:\s*</?[\w-]+[^>]*>\s*)+$")
# Claude Code wraps injected context in paired tags anywhere in a message.
CLAUDE_BLOCK_RE = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|local-command-stderr|"
    r"ide_opened_file|ide_selection|task-notification|command-name|command-message|command-args)>"
    r".*?</\1>",
    re.S,
)
HANDOFF_BANNER_RE = re.compile(r"^\[handoff\] Context transferred from (\w+)", re.M)
HANDOFF_SESSION_RE = re.compile(r"(?:Original session|- Session):\s*`?([0-9a-fA-F-]{8,})`?")


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def clean_user_text(text: str) -> tuple[str, str]:
    """Return (clean, fallback).

    clean    – what the user typed with injected blocks removed ('' if none)
    fallback – tag-stripped text of an unknown wrapper block, or ''
    """
    t = (text or "").strip()
    if not t or TAG_ONLY_RE.match(t):
        return "", ""
    t = CLAUDE_BLOCK_RE.sub("", t).strip()
    if not t:
        return "", ""
    # A "## My request:" section wins over whatever wrapper precedes it
    # (browser context, referenced conversations, attachment lists).
    sections = MY_REQUEST_RE.split(t)
    if len(sections) > 1 and sections[-1].strip():
        t = sections[-1].strip()
    m = LEADING_TAG_RE.match(t)
    if m:
        tag = m.group(1).lower()
        if tag == "realtime_delegation":
            said = [s.strip() for s in re.findall(r"^\s*user:\s*(.+)$", t, re.M) if s.strip()]
            return ("🎙 " + " / ".join(said[:3])) if said else "", ""
        if tag in BOILERPLATE_TAGS or tag.startswith("image") or tag.startswith("permissions"):
            return "", ""
        stripped = re.sub(r"<[^>]+>", " ", t)
        return "", squash(stripped)
    if t.startswith("# AGENTS.md instructions"):
        return "", ""
    sections = MY_REQUEST_RE.split(t)
    if len(sections) > 1:
        request = sections[-1].strip()
        if request:
            t = request
        else:
            pm = PASTED_PREVIEW_RE.search(t)
            if not pm:
                return "", ""
            t = "Pasted: " + pm.group(1)
    t = REFERENCED_SECTION_RE.sub("", t)
    t = FILES_BLOCK_RE.sub("", t)
    for sentence in WRAPPER_SENTENCES:
        t = t.replace(sentence, "")
    t = PLUGIN_MENTION_RE.sub(r"@\1", t)
    t = MD_LINK_RE.sub(r"\1", t)
    return t.strip(), ""


def clean_for_preview(author: str, text: str) -> str:
    """Multi-line cleaning for the preview pane (keeps paragraphs)."""
    if author == "user":
        clean, fallback = clean_user_text(text)
        return clean or fallback
    t = CLAUDE_BLOCK_RE.sub("", text or "")
    return t.strip()


def first_line(text: str, limit: int = 110) -> str:
    s = squash(text)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def handoff_lineage(text: str) -> tuple[str, str] | None:
    """If this message is a handoff banner, return (from_agent, source_session_id)."""
    if not text or "[handoff]" not in text:
        return None
    m = HANDOFF_BANNER_RE.search(text)
    if not m:
        return None
    sid = HANDOFF_SESSION_RE.search(text)
    return m.group(1).lower(), (sid.group(1) if sid else "")


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-")
