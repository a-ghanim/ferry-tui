"""The only module that knows how agent stores are laid out on disk.

Parsing of message content is delegated to `handoff`'s extractors; what this
module adds is what handoff does not expose: which Codex rollouts are the
user's own threads (vs guardian reviews and sub-agents), native titles,
workspace roots, the desktop/CLI split for Claude, and version gating so an
unknown format is a loud error, never a silent empty list.

Everything downstream (index, TUI, MCP) consumes `Instance` objects and
`Turn` lists from here and never touches the raw files.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from handoff.agents.base import (
    SessionRef,
    get_extractor,
    get_injector,
    known_agents,
    load_plugins,
)
from handoff.canonical import CanonicalTranscript, strip_infra
from handoff.redact import Redactor
from handoff.config import load_config

from ferry.cleaning import clean_for_preview, clean_user_text, handoff_lineage, squash
from ferry.paths import CACHE_DIR, HOME, LEGACY_PICK_TITLES

log = logging.getLogger(__name__)

COWORK_HOME = HOME / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"

# Formats we have verified against real files. Bump when a vendor change is
# confirmed compatible; anything newer is reported as "unverified" rather
# than parsed blindly.
KNOWN_CODEX_META_KEYS = {"id", "timestamp", "cwd"}
KNOWN_CODEX_MAJOR = 0  # rollouts verified against Codex 0.1xx
KNOWN_CLAUDE_MAJOR = 2

# First lines of the prompts ferry (and pick before it) sends to `claude -p`.
# Sessions that start this way are ours, not the user's.
FERRY_OWN_PROMPTS = (
    "Below is the beginning of a coding-assistant session.",
    "Below are the user's messages from one AI-assistant conversation.",
    "Summarize this coding-assistant conversation for an assistant that will continue it.",
)


class FormatError(Exception):
    """Raised when an entire store is unreadable. Individual files become AdapterIssue rows."""


@dataclass
class AdapterIssue:
    agent: str
    path: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.agent}] {self.path}: {self.field} — {self.message}"


@dataclass
class Turn:
    n: int
    author: str  # "user" | "agent"
    ts: str
    content: str


@dataclass
class Instance:
    """One run of one agent. Several instances can form one conversation."""

    agent: str  # handoff registry name: claude, codex, opencode, claude-cowork
    flavor: str  # claude-cli, claude-desktop, claude-cowork, codex, opencode
    session_id: str
    path: Path
    paths: list[Path]
    cwd: str
    roots: list[str]
    started: datetime
    updated: datetime
    mtime: float
    size: int
    native_title: str | None = None
    first_message: str = ""
    parent_hint: tuple[str, str] | None = None  # (from_agent, source_session_id)
    openable: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.agent}:{self.session_id}"


@dataclass
class Extracted:
    turns: list[Turn]
    files_touched: list[str]
    tokens_raw: int
    tokens_compact: int
    user_msgs: list[str]
    assistant_first: str
    transcript: CanonicalTranscript


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def agents() -> list[str]:
    load_plugins()
    return known_agents()


def home_for(agent: str) -> Path:
    if agent == "claude-cowork":
        return Path(os.environ.get("FERRY_COWORK_HOME", str(COWORK_HOME))).expanduser()
    cfg = load_config()
    env = os.environ.get(f"FERRY_{agent.upper().replace('-', '_')}_HOME")
    if env:
        return Path(env).expanduser()
    return cfg.home_for(agent)


def can_inject(agent: str) -> bool:
    try:
        get_injector(agent, home_for(agent))
    except Exception:
        return False
    return True


CLI_FOR = {"claude": "claude", "codex": "codex", "opencode": "opencode"}


def is_available(agent: str) -> bool:
    """An agent is present on this machine if its store exists or its CLI is on PATH.

    Codex counts as present when its sessions dir exists (the app, not the
    CLI, is what most people have). Cowork is read-only and never a target.
    """
    import shutil

    home = home_for(agent)
    if agent == "codex":
        return (home / "sessions").exists() or bool(shutil.which("codex"))
    if agent == "claude":
        return (home / "projects").exists() or bool(shutil.which("claude"))
    if agent == "claude-cowork":
        return home.exists()
    cli = CLI_FOR.get(agent)
    return home.exists() or bool(cli and shutil.which(cli))


def can_open(agent: str) -> bool:
    """Ferry can open an existing native session for this agent."""
    import shutil

    if agent == "claude":
        return bool(shutil.which("claude"))
    if agent == "codex":
        return bool(shutil.which("codex")) or ((home_for("codex") / "sessions").exists() and bool(shutil.which("open")))
    cli = CLI_FOR.get(agent)
    return bool(cli and shutil.which(cli))


def can_open_handoff(agent: str) -> bool:
    """Ferry can open a newly generated handoff in this agent."""
    import shutil

    if agent == "codex":
        return bool(shutil.which("codex"))
    return can_open(agent)


def agent_label(agent: str) -> str:
    return {
        "claude": "Claude Code",
        "codex": "Codex",
        "opencode": "OpenCode",
        "claude-cowork": "Claude Cowork",
    }.get(agent, agent.title())


def agent_icon(agent: str) -> str:
    return {"claude": "✳", "codex": "◎", "opencode": "○", "claude-cowork": "✳"}.get(agent, "•")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def iter_jsonl(path: Path) -> Iterator[dict]:
    """Tolerates a truncated last line (agents append while we read)."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _mtime_dt(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _is_ferry_own(cwd: str | None) -> bool:
    """Sessions ferry/pick created themselves (title generation via `claude -p`)."""
    if not cwd:
        return False
    for own in (CACHE_DIR, LEGACY_PICK_TITLES.parent):
        try:
            if Path(cwd).resolve() == own.resolve():
                return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
def codex_is_real(meta: dict) -> bool:
    source = meta.get("source")
    if isinstance(source, dict):  # {"subagent": {...}} — guardian reviews, thread spawns
        return False
    thread_source = str(meta.get("thread_source") or "").lower()
    if thread_source in {"guardian_review", "subagent"} or "review" in thread_source:
        return False
    if meta.get("parent_thread_id") and thread_source not in {"user", "chatgpt_handoff", "realtime_voice", ""}:
        return False
    return True


def codex_roots_from_env_block(text: str) -> list[str]:
    import re

    roots = re.findall(r"<root>(.*?)</root>", text or "", re.S)
    if not roots:
        m = re.search(r"<cwd>(.*?)</cwd>", text or "", re.S)
        roots = [m.group(1)] if m else []
    return [r.strip() for r in roots if r.strip()]


def codex_index_titles(home: Path) -> dict[str, str]:
    """Latest thread_name per id from session_index.jsonl (what the Codex app shows)."""
    idx = home / "session_index.jsonl"
    out: dict[str, tuple[str, str]] = {}
    if not idx.exists():
        return {}
    for rec in iter_jsonl(idx):
        sid = rec.get("id")
        name = str(rec.get("thread_name") or "").strip()
        if not sid or not name:
            continue
        stamp = str(rec.get("updated_at") or "")
        prev = out.get(sid)
        if prev is None or stamp >= prev[0]:
            out[sid] = (stamp, name)
    return {k: v[1] for k, v in out.items()}


def discover_codex(home: Path, issues: list[AdapterIssue]) -> list[Instance]:
    root = home / "sessions"
    if not root.exists():
        raise FormatError(f"No Codex sessions directory at {root} — is Codex installed?")
    files = sorted(root.glob("*/*/*/rollout-*.jsonl"))
    if not files:
        raise FormatError(f"No rollout files under {root} (expected sessions/YYYY/MM/DD/rollout-*.jsonl)")
    titles = codex_index_titles(home)
    by_id: dict[str, Instance] = {}
    for path in files:
        first = None
        for rec in iter_jsonl(path):
            first = rec
            break
        if first is None:
            continue  # empty/truncated file; nothing to gate on yet
        if first.get("type") != "session_meta":
            issues.append(AdapterIssue("codex", str(path), "type", f"first record is {first.get('type')!r}, expected 'session_meta'"))
            continue
        meta = first.get("payload") or {}
        missing = KNOWN_CODEX_META_KEYS - set(meta)
        if missing:
            issues.append(AdapterIssue("codex", str(path), "payload", f"session_meta missing {sorted(missing)}"))
            continue
        if not codex_is_real(meta):
            continue
        ver = str(meta.get("cli_version") or "")
        if ver:
            try:
                if int(ver.split(".")[0]) > KNOWN_CODEX_MAJOR:
                    issues.append(AdapterIssue("codex", str(path), "cli_version", f"rollout written by Codex {ver}; ferry verified up to {KNOWN_CODEX_MAJOR}.x — parsed best-effort"))
            except ValueError:
                pass
        sid = str(meta["id"])
        created = parse_ts(meta.get("timestamp")) or _mtime_dt(path)
        st = path.stat()
        updated = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        inst = by_id.get(sid)
        if inst is None:
            by_id[sid] = Instance(
                agent="codex",
                flavor="codex",
                session_id=sid,
                path=path,
                paths=[path],
                cwd=str(meta.get("cwd") or ""),
                roots=[str(meta["cwd"])] if meta.get("cwd") else [],
                started=created,
                updated=updated,
                mtime=st.st_mtime,
                size=st.st_size,
                native_title=titles.get(sid),
                extra={"originator": meta.get("originator"), "thread_source": meta.get("thread_source")},
            )
            continue
        inst.paths.append(path)
        inst.size += st.st_size
        if created < inst.started:
            inst.started = created
            inst.path = path  # earliest rollout holds the real first message
        if updated > inst.updated:
            inst.updated = updated
            inst.mtime = st.st_mtime
            if meta.get("cwd"):
                inst.cwd = str(meta["cwd"])
        cwd = str(meta.get("cwd") or "")
        if cwd and cwd not in inst.roots:
            inst.roots.append(cwd)
    for inst in by_id.values():
        inst.paths.sort()
    return list(by_id.values())


# --------------------------------------------------------------------------- #
# Claude Code (CLI store, also used by the desktop app's Code tab)
# --------------------------------------------------------------------------- #
def discover_claude(home: Path, issues: list[AdapterIssue]) -> list[Instance]:
    root = home / "projects"
    if not root.exists():
        raise FormatError(f"No Claude Code projects directory at {root} — is Claude Code installed?")
    out: list[Instance] = []
    files = [p for d in root.iterdir() if d.is_dir() for p in d.glob("*.jsonl")]
    for path in files:
        sid = path.stem
        cwd = ""
        first_ts = last_ts = ""
        version = ""
        entrypoint = ""
        custom_title = None
        ai_title = None
        n_user = 0
        n_assistant = 0
        first_user = ""
        parent_hint = None
        git_branch = None
        bad = False
        for rec in iter_jsonl(path):
            rtype = rec.get("type")
            if rtype in ("user", "assistant"):
                if "sessionId" not in rec or "message" not in rec:
                    issues.append(AdapterIssue("claude", str(path), rtype, "record lacks sessionId/message — format changed?"))
                    bad = True
                    break
                sid = rec.get("sessionId") or sid
                ts = rec.get("timestamp") or ""
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                cwd = cwd or rec.get("cwd") or ""
                version = version or rec.get("version") or ""
                entrypoint = entrypoint or rec.get("entrypoint") or ""
                git_branch = git_branch or rec.get("gitBranch")
                if rtype == "user" and not rec.get("isMeta"):
                    text = _claude_user_text(rec.get("message"))
                    if text:
                        clean, fb = clean_user_text(text)
                        if clean or fb:
                            n_user += 1
                            if not first_user:
                                first_user = clean or fb
                                lin = handoff_lineage(text)
                                if lin:
                                    parent_hint = lin
                elif rtype == "assistant":
                    n_assistant += 1
            elif rtype == "custom-title":
                custom_title = rec.get("customTitle") or custom_title
            elif rtype == "ai-title":
                ai_title = rec.get("aiTitle") or ai_title
        if bad:
            continue
        if n_user == 0:
            continue  # empty shells, /clear leftovers
        if _is_ferry_own(cwd) or first_user.startswith(FERRY_OWN_PROMPTS):
            continue  # ferry/pick's own `claude -p` title and tag calls
        if version:
            try:
                major = int(str(version).split(".")[0])
            except ValueError:
                major = KNOWN_CLAUDE_MAJOR
            if major > KNOWN_CLAUDE_MAJOR:
                issues.append(AdapterIssue("claude", str(path), "version", f"session written by Claude Code {version}; ferry verified up to {KNOWN_CLAUDE_MAJOR}.x — parsed best-effort"))
        st = path.stat()
        started = parse_ts(first_ts) or _mtime_dt(path)
        updated = parse_ts(last_ts) or _mtime_dt(path)
        flavor = "claude-desktop" if entrypoint == "claude-desktop" else "claude-cli"
        out.append(
            Instance(
                agent="claude",
                flavor=flavor,
                session_id=sid,
                path=path,
                paths=[path],
                cwd=cwd,
                roots=[cwd] if cwd else [],
                started=started,
                updated=updated,
                mtime=st.st_mtime,
                size=st.st_size,
                native_title=custom_title or ai_title,
                first_message=squash(first_user),
                parent_hint=parent_hint,
                extra={"version": version, "entrypoint": entrypoint, "git_branch": git_branch, "turns_hint": n_user + n_assistant},
            )
        )
    return out


def _claude_user_text(message) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Generic path for any other registered agent (opencode, plugins)
# --------------------------------------------------------------------------- #
def discover_generic(agent: str, home: Path, issues: list[AdapterIssue]) -> list[Instance]:
    try:
        extractor = get_extractor(agent, home)
        refs = extractor.list_sessions()
    except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
        raise FormatError(f"{agent}: {exc}") from exc
    out: list[Instance] = []
    for ref in refs:
        path = Path(ref.path)
        try:
            st = path.stat()
        except OSError as exc:
            issues.append(AdapterIssue(agent, str(path), "path", str(exc)))
            continue
        extra = {}
        openable = True
        if agent == "claude-cowork":
            openable = False
            extra = {"note": "Cowork sessions can be read, not reopened. Tab to Claude Code to continue one."}
        out.append(
            Instance(
                agent=agent,
                flavor=agent,
                session_id=ref.session_id,
                path=path,
                paths=[path],
                cwd=ref.cwd or "",
                roots=[ref.cwd] if ref.cwd else [],
                started=parse_ts(ref.created_at) or _mtime_dt(path),
                updated=parse_ts(ref.last_activity) or _mtime_dt(path),
                mtime=st.st_mtime,
                size=st.st_size,
                native_title=ref.title,
                openable=openable,
                extra=extra,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Discovery entry point
# --------------------------------------------------------------------------- #
@dataclass
class Discovery:
    instances: list[Instance]
    issues: list[AdapterIssue]
    store_errors: dict[str, str]  # agent -> why the whole store failed
    agents: list[str]


def discover(only: list[str] | None = None) -> Discovery:
    issues: list[AdapterIssue] = []
    store_errors: dict[str, str] = {}
    instances: list[Instance] = []
    names = [a for a in agents() if not only or a in only]
    for agent in names:
        try:
            home = home_for(agent)
            if agent == "codex":
                instances.extend(discover_codex(home, issues))
            elif agent == "claude":
                instances.extend(discover_claude(home, issues))
            else:
                instances.extend(discover_generic(agent, home, issues))
        except FormatError as exc:
            store_errors[agent] = str(exc)
        except Exception as exc:  # noqa: BLE001
            store_errors[agent] = f"{type(exc).__name__}: {exc}"
            log.exception("discover %s", agent)
    claude_ids = {i.session_id for i in instances if i.agent == "claude"}
    instances = [i for i in instances if not (i.agent == "claude-cowork" and i.session_id in claude_ids)]
    instances.sort(key=lambda i: i.updated, reverse=True)
    return Discovery(instances=instances, issues=issues, store_errors=store_errors, agents=names)


# --------------------------------------------------------------------------- #
# Content extraction (through handoff)
# --------------------------------------------------------------------------- #
def _ref_for(inst: Instance, path: Path) -> SessionRef:
    return SessionRef(
        session_id=inst.session_id,
        path=path,
        cwd=inst.cwd or None,
        created_at=inst.started.isoformat().replace("+00:00", "Z"),
        last_activity=inst.updated.isoformat().replace("+00:00", "Z"),
        message_count=0,
        title=inst.native_title,
    )


def extract_transcript(inst: Instance) -> CanonicalTranscript:
    """Canonical transcript for the whole instance (all rollouts, in order)."""
    extractor = get_extractor(inst.agent, home_for(inst.agent))
    merged: CanonicalTranscript | None = None
    for path in inst.paths:
        t = extractor.extract(_ref_for(inst, path))
        if merged is None:
            merged = t
        else:
            merged.transcript.extend(t.transcript)
            merged.artifacts.files_modified = sorted(set(merged.artifacts.files_modified) | set(t.artifacts.files_modified))
            if t.artifacts.task_state:
                merged.artifacts.task_state = t.artifacts.task_state
    assert merged is not None
    merged.metadata.message_count = len(merged.transcript)
    return merged


_REDACTOR = Redactor()


def extract(inst: Instance) -> Extracted:
    t = extract_transcript(inst)
    _REDACTOR.redact_transcript(t)  # secrets never reach the index
    tokens_raw = sum(len(m.content or "") for m in t.transcript) // 4
    tokens_compact = sum(len(m.content or "") for m in t.transcript if m.type == "message") // 4
    turns: list[Turn] = []
    user_msgs: list[str] = []
    assistant_first = ""
    pending_images = 0
    for m in t.transcript:
        if m.type != "message" or m.author not in ("user", "agent"):
            continue
        text = clean_for_preview(m.author, m.content or "")
        if m.author == "user" and not text and "input_image" in (m.content or ""):
            pending_images += 1
        if not text:
            continue
        if m.author == "user":
            if pending_images:
                text = f"[{pending_images} image{'s' if pending_images != 1 else ''}] " + text
                pending_images = 0
            user_msgs.append(squash(text))
        elif not assistant_first:
            assistant_first = squash(text)
        turns.append(Turn(n=len(turns), author=m.author, ts=m.timestamp, content=text))
    if inst.agent == "codex" and not inst.roots:
        pass
    # Workspace roots Codex recorded along the way (repos added mid-session).
    if inst.agent == "codex":
        codex_home = str(home_for("codex"))
        for m in t.transcript:
            if m.author == "user" and "<environment_context>" in (m.content or ""):
                for r in codex_roots_from_env_block(m.content):
                    # Codex lists its own scratch areas (visualizations, tmp) as roots; only the
                    # ChatGPT project mirrors under ~/.codex are places the user actually works.
                    if r.startswith(codex_home) and ".chatgpt-projects" not in r:
                        continue
                    if r not in inst.roots:
                        inst.roots.append(r)
        if not inst.first_message and user_msgs:
            inst.first_message = user_msgs[0]
        for m in t.transcript[:3]:
            lin = handoff_lineage(m.content or "")
            if lin:
                inst.parent_hint = lin
                break
    if not inst.first_message and user_msgs:
        inst.first_message = user_msgs[0]
    return Extracted(
        turns=turns,
        files_touched=list(t.artifacts.files_modified),
        tokens_raw=tokens_raw,
        tokens_compact=tokens_compact,
        user_msgs=user_msgs,
        assistant_first=assistant_first,
        transcript=t,
    )


def compact_transcript(t: CanonicalTranscript) -> CanonicalTranscript:
    """Drop tool calls/results/reasoning; keep the conversation."""
    t.transcript = [m for m in t.transcript if m.type == "message"]
    t.metadata.message_count = len(t.transcript)
    return t


__all__ = [
    "AdapterIssue",
    "Discovery",
    "Extracted",
    "FormatError",
    "Instance",
    "Turn",
    "agent_icon",
    "agent_label",
    "agents",
    "can_inject",
    "compact_transcript",
    "discover",
    "extract",
    "extract_transcript",
    "home_for",
    "strip_infra",
]
