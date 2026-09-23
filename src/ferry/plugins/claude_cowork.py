"""handoff plugin: Claude Cowork (the desktop app's non-code half).

Store (macOS): ~/Library/Application Support/Claude/local-agent-mode-sessions/
    <account>/<device>/local_<uuid>.json      metadata: title, cwd, folders, times
    <account>/<device>/local_<uuid>/audit.jsonl  user/assistant/system/tool records

Reading is full. Writing is not possible — nothing external can make the
desktop app open a session — so the injector raises and ferry never offers
Cowork as a Tab target. Registered through handoff's `handoff.agents`
entry-point group; no fork of handoff involved.

The desktop app's *Code* tab writes to the normal Claude Code store
(~/.claude/projects) with `entrypoint: "claude-desktop"`; that is handled by
ferry's Claude adapter, not here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from handoff.agents.base import (
    Extractor,
    Injector,
    SessionRef,
    register_extractor,
    register_injector,
)
from handoff.canonical import Artifacts, CanonicalTranscript, Message, Metadata, now_iso


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def _ms_iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return ""


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
    return "\n".join(parts)


class CoworkExtractor(Extractor):
    agent_name = "claude-cowork"

    def _meta_files(self) -> list[Path]:
        if not self.home.exists():
            return []
        return sorted(self.home.glob("*/*/local_*.json"))

    def list_sessions(self, cwd: Path | None = None) -> list[SessionRef]:
        refs: list[SessionRef] = []
        cwd_str = str(Path(cwd).resolve()) if cwd else None
        for meta_path in self._meta_files():
            audit = meta_path.with_suffix("") / "audit.jsonl"
            if not audit.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict):
                continue
            if meta.get("isArchived"):
                continue
            folders = [str(f) for f in (meta.get("userSelectedFolders") or []) if isinstance(f, str)]
            session_cwd = folders[0] if folders else None
            if cwd_str and session_cwd != cwd_str:
                continue
            count = 0
            for rec in _iter_jsonl(audit):
                if rec.get("type") in ("user", "assistant"):
                    count += 1
            if count == 0:
                continue
            sid = str(meta.get("cliSessionId") or meta.get("sessionId") or meta_path.stem)
            refs.append(
                SessionRef(
                    session_id=sid,
                    path=audit,
                    cwd=session_cwd,
                    created_at=_ms_iso(meta.get("createdAt")),
                    last_activity=_ms_iso(meta.get("lastActivityAt")),
                    message_count=count,
                    title=str(meta.get("title") or "") or None,
                )
            )
        refs.sort(key=lambda r: r.last_activity or "", reverse=True)
        return refs

    def extract(self, ref: SessionRef) -> CanonicalTranscript:
        messages: list[Message] = []
        first_ts = ""
        last_ts = ""
        for idx, rec in enumerate(_iter_jsonl(Path(ref.path))):
            rtype = rec.get("type")
            ts = rec.get("timestamp") or now_iso()
            if rtype not in ("user", "assistant"):
                continue
            first_ts = first_ts or ts
            last_ts = ts
            msg = rec.get("message") or {}
            content = msg.get("content")
            mid = rec.get("uuid") or f"cowork-{idx}"
            if rtype == "user":
                if isinstance(content, list):
                    for p in content:
                        if isinstance(p, dict) and p.get("type") == "tool_result":
                            messages.append(
                                Message(
                                    id=mid,
                                    timestamp=ts,
                                    author="system",
                                    type="tool_result",
                                    content=_text(p.get("content")),
                                    metadata={"call_id": p.get("tool_use_id")},
                                )
                            )
                text = _text(content)
                if text.strip():
                    messages.append(Message(id=mid, timestamp=ts, author="user", type="message", content=text))
            else:
                if isinstance(content, list):
                    for p in content:
                        if not isinstance(p, dict):
                            continue
                        if p.get("type") == "text" and str(p.get("text") or "").strip():
                            messages.append(Message(id=mid, timestamp=ts, author="agent", type="message", content=str(p["text"])))
                        elif p.get("type") == "tool_use":
                            messages.append(
                                Message(
                                    id=mid,
                                    timestamp=ts,
                                    author="agent",
                                    type="tool_call",
                                    content=json.dumps(p.get("input") or {}, ensure_ascii=False),
                                    metadata={"tool_name": p.get("name") or "tool", "call_id": p.get("id")},
                                )
                            )
                elif isinstance(content, str) and content.strip():
                    messages.append(Message(id=mid, timestamp=ts, author="agent", type="message", content=content))
        meta = Metadata(
            session_id=ref.session_id,
            source_agent="claude-cowork",
            source_session_path=str(ref.path),
            created_at=ref.created_at or first_ts or now_iso(),
            last_activity=ref.last_activity or last_ts or now_iso(),
            message_count=len(messages),
            cwd=ref.cwd or "",
        )
        return CanonicalTranscript(metadata=meta, transcript=messages, artifacts=Artifacts())


class CoworkInjector(Injector):
    agent_name = "claude-cowork"

    def inject(self, transcript: CanonicalTranscript) -> Path:  # pragma: no cover - by design
        raise NotImplementedError(
            "Claude Cowork sessions cannot be created from outside the desktop app. "
            "Hand the conversation to Claude Code instead."
        )


def register() -> None:
    register_extractor("claude-cowork", lambda home: CoworkExtractor(home))
    # No injector on purpose: ferry checks `can_inject` and hides Cowork as a target.


register()
