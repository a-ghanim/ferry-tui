"""Tags: ferry's own label on a conversation. Never called "project".

Inferred, in order: every workspace root the session ever had; absolute paths
under the home dir mentioned in the user's turns; a `claude -p` tag as last
resort (cached, on demand only — never inside the index build). User edits
persist in the index and win over inference.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ferry.cleaning import slugify
from ferry.paths import HOME, TAGS_CACHE, ensure_cache_dir, is_scratch_dir

CONTAINER_DIRS = {"documents", "code", "projects", "developer", "src", "repos", "work", "dev", "github", "desktop", "chatgpt", "codex", "claude"}
GENERIC = {"new-project", "project", "untitled", "tmp", "temp", "scratch", "downloads", "home", ""}
PATH_RE = re.compile(r"(?:~|/Users/[^/\s]+)/((?:[^/\s\"'`<>|;]+/){1,3}[^/\s\"'`<>|;]+)")


def normalise(tag: str) -> str:
    return slugify(tag)[:40]


def _project_name_from_mirror(root: Path) -> str | None:
    """ChatGPT project mirrors (~/.codex/.chatgpt-projects/g-p-<hash>) carry the real name in AGENTS.md."""
    agents = root / "AGENTS.md"
    try:
        text = agents.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"ChatGPT project\s+[“\"']([^”\"']+)[”\"']", text)
    return m.group(1) if m else None


def tag_for_folder(folder: str | None) -> str | None:
    if not folder or is_scratch_dir(folder):
        return None
    p = Path(folder)
    if ".chatgpt-projects" in p.parts:
        name = _project_name_from_mirror(p)
        return normalise(name) if name else None
    name = p.name
    if name.lower() in CONTAINER_DIRS and p.parent != p:
        return None
    tag = normalise(name)
    return None if tag in GENERIC or re.fullmatch(r"[0-9a-f-]{16,}", tag or "") else tag


def tags_from_text(text: str, limit: int = 4) -> list[str]:
    found: list[str] = []
    for m in PATH_RE.finditer(text or ""):
        parts = [s for s in m.group(1).split("/") if s]
        if any(s.startswith(".") for s in parts):
            continue  # dotfile trees (~/.codex, ~/.cache, ~/.local) are tooling, not topics
        # Walk past container dirs ("Documents/ChatGPT/Example" → Example).
        pick = None
        for i, seg in enumerate(parts):
            if seg.lower() in CONTAINER_DIRS:
                continue
            if re.fullmatch(r"20\d\d-\d\d-\d\d", seg):  # Codex scratch date dirs
                pick = None
                break
            pick = seg
            break
        if not pick or "." in pick and not pick.startswith("."):
            continue  # a file, not a folder
        tag = normalise(pick)
        if tag and tag not in GENERIC and tag not in found and not re.fullmatch(r"[0-9a-f-]{16,}", tag):
            found.append(tag)
        if len(found) >= limit:
            break
    return found


def infer_from_instances(roots_and_cwds: list[tuple[list[str], str]], user_text: str) -> list[str]:
    tags: list[str] = []
    for roots, cwd in roots_and_cwds:
        for folder in list(roots) + [cwd]:
            t = tag_for_folder(folder)
            if t and t not in tags:
                tags.append(t)
    for t in tags_from_text(user_text):
        if t not in tags:
            tags.append(t)
    return tags


# ---- LLM tags (last resort, cached, on demand) ----------------------------- #
def _load() -> dict:
    ensure_cache_dir()
    try:
        data = json.loads(TAGS_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    ensure_cache_dir()
    tmp = TAGS_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(TAGS_CACHE)


def cached_tags(conv_id: str) -> list[str]:
    entry = _load().get(conv_id)
    if isinstance(entry, dict) and isinstance(entry.get("tags"), list):
        return [normalise(t) for t in entry["tags"] if normalise(t)]
    return []


TAG_PROMPT = (
    "Below are the user's messages from one AI-assistant conversation. Give 1 to 3 short "
    "topic tags that say what this conversation is about (a product, a client, a document, "
    "a subject). Lowercase, hyphenated, no '#', comma-separated, nothing else.\n"
    "Existing tags in this archive you may reuse when they fit: {existing}\n\n"
    "<messages>\n{text}\n</messages>"
)


def generate_tags(conv_id: str, user_text: str, existing: list[str]) -> list[str] | None:
    from ferry.titles import run_claude_p

    if not user_text.strip():
        return None
    out = run_claude_p(TAG_PROMPT.format(existing=", ".join(existing[:40]) or "(none)", text=user_text[:6000]))
    if out is None:
        return None
    line = next((ln for ln in out.strip().splitlines() if ln.strip()), "")
    tags = []
    for raw in re.split(r"[,\n]", line):
        t = normalise(raw.strip().lstrip("#-• ").strip())
        if t and t not in tags and len(t) <= 30:
            tags.append(t)
    tags = tags[:3]
    if not tags:
        return None
    data = _load()
    data[conv_id] = {"tags": tags, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "generator": "claude -p"}
    _save(data)
    return tags
