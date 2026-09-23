"""Where ferry keeps its own state. Everything lives under one cache dir.

Nothing here is precious: the index is rebuilt from the agents' stores, the
title/tag caches are conveniences, and the manifest exists so `ferry undo`
can reverse the last write. Deleting the directory loses nothing that the
agents themselves still have.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CACHE_DIR = Path(os.environ.get("FERRY_CACHE", "~/.cache/ferry")).expanduser()
INDEX_DB = CACHE_DIR / "index.db"
TITLES_CACHE = CACHE_DIR / "titles.json"
TAGS_CACHE = CACHE_DIR / "tags.json"
WRITES_MANIFEST = CACHE_DIR / "writes.jsonl"
ARCHIVE_DIR = CACHE_DIR / "archive"
PREFS_FILE = CACHE_DIR / "prefs.json"  # per-conversation folder choices etc.

# pick (ferry's predecessor) kept its title cache here; reuse it once.
LEGACY_PICK_TITLES = Path("~/.cache/pick/titles.json").expanduser()

HOME = Path.home()


def ensure_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CACHE_DIR, 0o700)
    except OSError:
        pass
    if not os.environ.get("FERRY_CACHE") and LEGACY_PICK_TITLES.exists() and not TITLES_CACHE.exists():
        try:
            shutil.copyfile(LEGACY_PICK_TITLES, TITLES_CACHE)
        except OSError:
            pass
    return CACHE_DIR


def display_path(p: str | os.PathLike | None) -> str:
    """Home-relative, hash segments elided. Never shows a raw id."""
    if not p:
        return ""
    s = str(p)
    home = str(HOME)
    if s == home:
        return "~"
    if s.startswith(home + "/"):
        s = "~" + s[len(home):]
    parts = s.split("/")
    out = []
    for seg in parts:
        if _HASHY.match(seg):
            out.append("…")
        else:
            out.append(seg)
    return "/".join(out)


import re as _re

_HASHY = _re.compile(r"^(?:g-p-)?[0-9a-f]{16,}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$|^scratch-\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$", _re.IGNORECASE)


def is_scratch_dir(p: str | None) -> bool:
    """Directories agents invent when the user did not choose a folder."""
    if not p:
        return True
    s = str(p)
    home = str(HOME)
    if s == home:
        return True
    markers = (
        "/Documents/Codex/20",  # ~/Documents/Codex/<date>/<slug>
        "/Library/Application Support/Claude/scratch-workspaces/",
        "/Library/Application Support/Claude/local-agent-mode-sessions/",
        "/.cache/ferry",
        "/.cache/pick",
        "/private/tmp",
        "/tmp",
    )
    return any(m in s for m in markers)
