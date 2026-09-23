"""Tab: carry a conversation across to another agent, safely.

Rules (DESIGN.md "Tab safety rules"):
- Never touch originals. Every write is a new file in the target store, logged
  in ~/.cache/ferry/writes.jsonl so `ferry undo` can reverse it.
- Refuse to move a live session (source modified in the last 30 s).
- Validate before handing over: re-read the generated file with the target
  agent's own extractor; refuse if it does not round-trip.
- Folders inherit, permissions don't: the target folder is passed as the
  new instance's cwd and nothing is pre-approved.
- Secrets are redacted with handoff's Redactor. Boilerplate is stripped with
  handoff's strip_infra.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from handoff.agents.base import SessionRef, get_extractor, get_injector
from handoff.canonical import CanonicalTranscript, Message, now_iso, strip_infra
from handoff.redact import Redactor

from ferry import adapters
from ferry.adapters import agent_label, compact_transcript, home_for
from ferry.cleaning import clean_for_preview
from ferry.index import ConversationRow, Index, InstanceRow
from ferry.paths import ARCHIVE_DIR, WRITES_MANIFEST, ensure_cache_dir
from ferry.titles import run_claude_p

LIVE_WINDOW_SECONDS = 30
COMPACT_THRESHOLD_TOKENS = 60_000
MODES = ("raw", "compact", "brief")


class TransferError(Exception):
    pass


class LiveSessionError(TransferError):
    pass


@dataclass
class MoveResult:
    to_agent: str
    new_session_id: str
    new_instance_id: str
    path: Path
    mode: str
    folders: list[str]
    turns: int
    banner: str
    conversation_id: str


@dataclass
class ManifestEntry:
    ts: str
    action: str  # inject | archive | rename | undo
    agent: str
    paths: list[str]
    session_id: str = ""
    parent_instance_id: str = ""
    conversation_id: str = ""
    mode: str = ""
    folders: list[str] = field(default_factory=list)
    sqlite_rows: list[dict] = field(default_factory=list)
    original_paths: list[str] = field(default_factory=list)
    undone: bool = False
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)


def _manifest_append(entry: ManifestEntry) -> None:
    ensure_cache_dir()
    with WRITES_MANIFEST.open("a", encoding="utf-8") as fh:
        fh.write(entry.to_json() + "\n")


def _manifest_read() -> list[ManifestEntry]:
    if not WRITES_MANIFEST.exists():
        return []
    out = []
    for line in WRITES_MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(ManifestEntry(**{k: v for k, v in d.items() if k in ManifestEntry.__dataclass_fields__}))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _manifest_rewrite(entries: list[ManifestEntry]) -> None:
    ensure_cache_dir()
    WRITES_MANIFEST.write_text("".join(e.to_json() + "\n" for e in entries), encoding="utf-8")


def choose_mode(tokens_raw: int) -> str:
    return "compact" if tokens_raw >= COMPACT_THRESHOLD_TOKENS else "raw"


def is_live(inst: InstanceRow) -> bool:
    try:
        newest = max(Path(p).stat().st_mtime for p in inst.paths if Path(p).exists())
    except ValueError:
        return False
    return (time.time() - newest) < LIVE_WINDOW_SECONDS


def targets_for(inst: InstanceRow) -> list[str]:
    """Agents that can receive this instance: registered, present on this machine, and writable."""
    return [
        a for a in adapters.agents()
        if a != inst.agent and adapters.is_available(a) and adapters.can_open_handoff(a) and adapters.can_inject(a)
    ]


def summary_line(inst: InstanceRow) -> str:
    tokens = inst.tokens_raw
    tok = f"~{tokens // 1000}k tokens" if tokens >= 1000 else f"~{tokens} tokens"
    files = f"{len(inst.files)} files touched" if inst.files else "no files touched"
    return f"{inst.turns} turns · {files} · {tok}"


def _brief_transcript(t: CanonicalTranscript, inst: InstanceRow, title: str) -> CanonicalTranscript:
    """Replace the conversation with one context message: a generated summary."""
    user_turns = [m.content for m in t.transcript if m.author == "user" and m.type == "message"]
    agent_turns = [m.content for m in t.transcript if m.author == "agent" and m.type == "message"]
    body = "\n\n".join(f"USER: {u[:800]}" for u in user_turns[-12:])
    if agent_turns:
        body += f"\n\nLAST ASSISTANT: {agent_turns[-1][:1500]}"
    generated = run_claude_p(
        "Summarize this coding-assistant conversation for an assistant that will continue it. "
        "Cover: the goal, what was decided, what was done (files, commands), what is still open, "
        "and any constraints the user stated. 150-300 words, plain prose, no preamble.\n\n"
        f"<conversation title=\"{title}\">\n{body[:12000]}\n</conversation>"
    )
    if not generated:
        generated = "Summary (generated without an LLM — claude -p was unavailable):\n" + "\n".join(
            f"- {u[:200]}" for u in user_turns[-8:]
        )
    files = "\n".join(f"- {f}" for f in inst.files[:40]) or "- (none recorded)"
    task = ""
    if t.artifacts.task_state and t.artifacts.task_state.items:
        task = "\n\nTask list at hand-off:\n" + "\n".join(f"- [{i.status}] {i.content}" for i in t.artifacts.task_state.items)
    text = f"# Brief: {title}\n\n{generated}\n\nFiles touched:\n{files}{task}"
    ts = now_iso()
    t.transcript = [Message(id="ferry-brief", timestamp=ts, author="user", type="message", content=text)]
    t.metadata.message_count = 1
    return t


def _clean_user_messages(t: CanonicalTranscript) -> None:
    """Unwrap harness wrappers inside user messages so the receiving agent (and its
    thread title, which injectors take from the first user message) sees what the
    human typed. Messages that were only boilerplate are dropped."""
    kept: list[Message] = []
    for m in t.transcript:
        if m.author == "user" and m.type == "message":
            text = clean_for_preview("user", m.content or "")
            if not text.strip():
                continue
            m.content = text
        kept.append(m)
    t.transcript = kept
    t.metadata.message_count = len(kept)


def _new_session_id(agent: str, path: Path) -> str:
    if agent == "codex":
        try:
            first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            return str(first.get("payload", {}).get("id") or path.stem)
        except (OSError, json.JSONDecodeError, IndexError):
            return path.stem
    return path.stem


def _validate_roundtrip(agent: str, path: Path, session_id: str, cwd: str) -> int:
    extractor = get_extractor(agent, home_for(agent))
    ref = SessionRef(session_id=session_id, path=path, cwd=cwd or None, created_at="", last_activity="", message_count=0)
    t = extractor.extract(ref)
    n_user = sum(1 for m in t.transcript if m.author == "user" and m.type == "message" and m.content.strip())
    if not t.transcript or n_user == 0:
        raise TransferError(f"generated {agent} session did not round-trip through the {agent} adapter ({len(t.transcript)} messages, {n_user} user)")
    return len(t.transcript)


def move(
    index: Index,
    conv: ConversationRow,
    inst: InstanceRow,
    to_agent: str,
    mode: str = "auto",
    folders: list[str] | None = None,
    force_live: bool = False,
) -> MoveResult:
    if to_agent == inst.agent:
        raise TransferError(f"{agent_label(to_agent)} is already where this instance lives")
    if not adapters.can_inject(to_agent):
        raise TransferError(f"{agent_label(to_agent)} sessions cannot be created from outside its app")
    if not force_live and is_live(inst):
        raise LiveSessionError("still active — finish the turn first (source changed in the last 30 s)")
    if mode not in MODES + ("auto",):
        raise TransferError(f"unknown mode {mode!r}; use raw, compact or brief")
    folders = [f for f in (folders or inst.roots or ([inst.cwd] if inst.cwd else [])) if f] or [str(Path.home())]
    for f in folders:
        if not Path(f).is_dir():
            raise TransferError(f"target folder does not exist: {f}")

    src = adapters.Instance(
        agent=inst.agent, flavor=inst.flavor, session_id=inst.session_id, path=Path(inst.path),
        paths=[Path(p) for p in inst.paths], cwd=inst.cwd, roots=list(inst.roots),
        started=datetime.fromisoformat(inst.started_at.replace("Z", "+00:00")) if inst.started_at else datetime.now(timezone.utc),
        updated=datetime.fromisoformat(inst.updated_at.replace("Z", "+00:00")) if inst.updated_at else datetime.now(timezone.utc),
        mtime=inst.mtime, size=inst.size, native_title=inst.native_title,
    )
    t = adapters.extract_transcript(src)
    strip_infra(t)
    _clean_user_messages(t)
    if mode == "auto":
        mode = choose_mode(inst.tokens_raw)
    if mode == "compact":
        compact_transcript(t)
    elif mode == "brief":
        _brief_transcript(t, inst, conv.title)
    Redactor().redact_transcript(t)
    t.metadata.cwd = folders[0]
    if not t.transcript:
        raise TransferError("nothing to move: the conversation has no messages after stripping boilerplate")

    injector = get_injector(to_agent, home_for(to_agent))
    path = injector.inject(t)
    new_sid = _new_session_id(to_agent, path)
    entry = ManifestEntry(
        ts=now_iso(), action="inject", agent=to_agent, paths=[str(path)], session_id=new_sid,
        parent_instance_id=inst.id, conversation_id=conv.id, mode=mode, folders=folders,
        sqlite_rows=[{"db": str(home_for("codex") / "state_5.sqlite"), "table": "threads", "id": new_sid}] if to_agent == "codex" else [],
    )
    _manifest_append(entry)
    try:
        n = _validate_roundtrip(to_agent, path, new_sid, folders[0])
    except Exception as exc:
        _quarantine(entry, reason=str(exc))
        raise TransferError(f"refused to hand over: {exc}") from exc

    index.rebuild(only=[to_agent])
    new_iid = f"{to_agent}:{new_sid}"
    index.record_lineage(new_iid, inst.id, inst.agent, to_agent)
    index.set_prefs(conv.id, folders=folders, mode=mode)
    banner = f"moved from {agent_label(inst.agent)} · {inst.turns} turns · {mode}"
    return MoveResult(
        to_agent=to_agent, new_session_id=new_sid, new_instance_id=new_iid, path=path, mode=mode,
        folders=folders, turns=n, banner=banner, conversation_id=conv.id,
    )


def _quarantine(entry: ManifestEntry, reason: str) -> None:
    """Move a bad generated file out of the agent's store (never delete) and mark the entry undone."""
    dest_dir = ARCHIVE_DIR / "failed" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir.mkdir(parents=True, exist_ok=True)
    for p in entry.paths:
        pp = Path(p)
        if pp.exists():
            shutil.move(str(pp), str(dest_dir / pp.name))
    _delete_sqlite_rows(entry)
    entries = _manifest_read()
    for e in entries:
        if e.ts == entry.ts and e.paths == entry.paths:
            e.undone = True
            e.note = f"quarantined: {reason}"
    _manifest_rewrite(entries)


def _delete_sqlite_rows(entry: ManifestEntry) -> None:
    for row in entry.sqlite_rows:
        db = Path(row.get("db", ""))
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(str(db))
            conn.execute(f"DELETE FROM {row['table']} WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
        except sqlite3.Error:
            continue


def undo(index: Index) -> str:
    entries = _manifest_read()
    for e in reversed(entries):
        if e.undone or e.action == "undo":
            continue
        if e.action == "inject":
            dest_dir = ARCHIVE_DIR / "undo" / datetime.now().strftime("%Y%m%d-%H%M%S")
            dest_dir.mkdir(parents=True, exist_ok=True)
            moved = []
            for p in e.paths:
                pp = Path(p)
                if pp.exists():
                    shutil.move(str(pp), str(dest_dir / pp.name))
                    moved.append(str(dest_dir / pp.name))
            _delete_sqlite_rows(e)
            e.undone = True
            e.note = f"undone → {dest_dir}"
            _manifest_rewrite(entries)
            index.rebuild(only=[e.agent])
            return f"undid hand-off to {agent_label(e.agent)}; file kept at {dest_dir}"
        if e.action == "archive":
            for src, orig in zip(e.paths, e.original_paths):
                if Path(src).exists():
                    Path(orig).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(src, orig)
            for row in e.sqlite_rows:
                _set_codex_archived(row.get("id", ""), False)
            e.undone = True
            _manifest_rewrite(entries)
            index.rebuild(only=[e.agent])
            return f"restored archived {agent_label(e.agent)} session"
        if e.action == "rename":
            e.undone = True
            _manifest_rewrite(entries)
            return "renames are append-only in the agents' stores; rename again to change the title"
    return "nothing to undo"


def _set_codex_archived(thread_id: str, archived: bool) -> None:
    db = home_for("codex") / "state_5.sqlite"
    if not db.exists() or not thread_id:
        return
    try:
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE threads SET archived=? WHERE id=?", (1 if archived else 0, thread_id))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def archive(index: Index, conv: ConversationRow, inst: InstanceRow) -> str:
    """Move the session file(s) to ~/.cache/ferry/archive/<agent>/. Never deletes."""
    if is_live(inst):
        raise LiveSessionError("still active — finish the turn first")
    dest_dir = ARCHIVE_DIR / inst.agent
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    originals: list[str] = []
    for p in inst.paths:
        pp = Path(p)
        if not pp.exists():
            continue
        dest = dest_dir / pp.name
        if dest.exists():
            dest = dest_dir / f"{pp.stem}.{int(time.time())}{pp.suffix}"
        shutil.move(str(pp), str(dest))
        moved.append(str(dest))
        originals.append(str(pp))
    rows = []
    if inst.agent == "codex":
        _set_codex_archived(inst.session_id, True)
        rows = [{"db": str(home_for("codex") / "state_5.sqlite"), "table": "threads", "id": inst.session_id}]
    _manifest_append(ManifestEntry(ts=now_iso(), action="archive", agent=inst.agent, paths=moved, original_paths=originals,
                                   session_id=inst.session_id, conversation_id=conv.id, sqlite_rows=rows))
    index.rebuild(only=[inst.agent])
    return f"archived to {dest_dir}"


def rename(index: Index, conv: ConversationRow, title: str) -> str:
    """Rename in the agent's own store where that store has a title, and in ferry."""
    title = " ".join(title.split())
    if not title:
        raise TransferError("empty title")
    inst = conv.newest
    where = "ferry only"
    if inst.agent == "codex":
        idx = home_for("codex") / "session_index.jsonl"
        with idx.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": inst.session_id, "thread_name": title, "updated_at": now_iso()}) + "\n")
        try:
            conn = sqlite3.connect(str(home_for("codex") / "state_5.sqlite"))
            conn.execute("UPDATE threads SET title=? WHERE id=?", (title, inst.session_id))
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass
        where = "Codex (session_index.jsonl)"
        index.set_native_title(inst.id, title)
    elif inst.agent == "claude":
        p = Path(inst.path)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": inst.session_id}) + "\n")
        where = "Claude Code (custom-title record)"
        index.set_native_title(inst.id, title)
    index.set_title_override(conv.id, title)
    _manifest_append(ManifestEntry(ts=now_iso(), action="rename", agent=inst.agent, paths=[inst.path], session_id=inst.session_id, conversation_id=conv.id, note=title))
    return where


def second_opinion(index: Index, conv: ConversationRow, inst: InstanceRow, mode: str = "auto") -> list[MoveResult]:
    """Fork the conversation to every other agent that can receive it (the `2` key)."""
    results = []
    for agent in targets_for(inst):
        results.append(move(index, conv, inst, agent, mode=mode, folders=conv.prefs.get("folders") or None))
    if not results:
        raise TransferError("no other agent can receive this conversation")
    return results


def manifest() -> list[ManifestEntry]:
    return _manifest_read()
