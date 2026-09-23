"""Titles: native where the agent has one, `claude -p` where it doesn't.

Ported from `pick`. The cache (~/.cache/ferry/titles.json) sits *beside* the
index, never inside it: a failed `claude -p` can never block a rebuild, and a
generated title is computed exactly once per session id.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from ferry.paths import CACHE_DIR, TITLES_CACHE, ensure_cache_dir

TITLE_PROMPT = (
    "Below is the beginning of a coding-assistant session. Write a title of "
    "6 to 10 words that says what the user is working on. Use Title Case, no "
    "quotes, no trailing punctuation. Output only the title.\n\n"
    "<session>\n{transcript}\n</session>"
)

_unavailable: str | None = None  # set once per process when claude -p cannot run


def claude_unavailable_reason() -> str | None:
    return _unavailable


def load_cache() -> dict:
    ensure_cache_dir()
    try:
        with TITLES_CACHE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    ensure_cache_dir()
    tmp = TITLES_CACHE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(TITLES_CACHE)


def cached_title(session_id: str) -> str | None:
    entry = load_cache().get(session_id)
    if isinstance(entry, dict) and entry.get("title"):
        return str(entry["title"])
    return None


def normalise_title(raw: str) -> str:
    line = ""
    for candidate in (raw or "").strip().splitlines():
        candidate = candidate.strip()
        if candidate:
            line = candidate
            break
    line = line.strip().strip("\"'`*#").strip()
    line = re.sub(r"^(title|summary)\s*:\s*", "", line, flags=re.IGNORECASE)
    line = line.rstrip(".!")
    return re.sub(r"\s+", " ", line).strip()


def fallback_title(first_message: str) -> str:
    words = (first_message or "").split()
    if not words:
        return "(untitled session)"
    return " ".join(words[:9]) + ("…" if len(words) > 9 else "")


def run_claude_p(prompt: str, timeout: int = 180) -> str | None:
    """Run `claude -p` with the prompt on stdin. Returns stdout or None.

    Runs from ferry's cache dir so Claude Code does not load any project
    context, and so the sessions it creates are recognisable (and hidden) by
    the index.
    """
    global _unavailable
    if os.environ.get("FERRY_OFFLINE") == "1":
        return None
    if _unavailable:
        return None
    claude = shutil.which("claude")
    if not claude:
        _unavailable = "claude CLI not found on PATH"
        return None
    ensure_cache_dir()
    cmd = [claude, "-p"]
    model = os.environ.get("FERRY_CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(CACHE_DIR),
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _unavailable = f"claude -p failed: {exc}"
        return None
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    blob = f"{out}\n{err}".lower()
    if proc.returncode != 0 or "not logged in" in blob or "failed to authenticate" in blob:
        reason = (out or err or f"exit {proc.returncode}").splitlines()[0][:80]
        _unavailable = f"claude -p unavailable ({reason}); run `claude login`"
        return None
    return out


def generate_title(session_id: str, user_msgs: list[str], assistant_first: str = "") -> str | None:
    """Generate, cache, and return a 6–10 word title; None if it cannot be done."""
    msgs = [m for m in user_msgs if m][:6]
    if not msgs:
        return None
    transcript = "\n".join(f"USER: {m[:600]}" for m in msgs)
    if assistant_first:
        transcript += f"\nASSISTANT: {assistant_first[:600]}"
    prompt = TITLE_PROMPT.format(transcript=transcript[:6000])
    title = ""
    for attempt in range(2):
        out = run_claude_p(prompt)
        if out is None:
            return None
        title = normalise_title(out)
        n = len(title.split())
        if 6 <= n <= 10:
            break
        if 3 <= n <= 14 and attempt == 1:
            break
        title = ""
    if not title:
        return None
    cache = load_cache()
    cache[session_id] = {
        "title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "claude -p",
    }
    save_cache(cache)
    return title


def warn_once_if_unavailable() -> None:
    if _unavailable:
        sys.stderr.write(f"ferry: {_unavailable}; showing first-message labels for untitled sessions\n")
