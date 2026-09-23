"""The archive: one SQLite file every consumer reads instead of raw session files.

Deterministic — same files in, same rows out. Rebuilt incrementally by
(mtime, size). No LLM calls in here; titles and tags that need one are
written *into* the index by commands, never computed by it.

Row model (see DESIGN.md "Data model"):
  conversation 1 ── n instance ── n turn
Instances are linked into conversations by lineage: a handoff banner in the
first message, or an explicit record from ferry's own transfer.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from ferry import adapters, tags as tagmod, titles
from ferry.adapters import AdapterIssue, Instance, Turn
from ferry.paths import INDEX_DB, ensure_cache_dir

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  created_at TEXT, updated_at TEXT,
  title TEXT, title_source TEXT,
  tags_json TEXT DEFAULT '[]',
  title_override TEXT,
  tag_add_json TEXT DEFAULT '[]', tag_remove_json TEXT DEFAULT '[]',
  prefs_json TEXT DEFAULT '{}',
  excluded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  agent TEXT, flavor TEXT, session_id TEXT,
  path TEXT, paths_json TEXT, cwd TEXT, roots_json TEXT,
  started_at TEXT, updated_at TEXT, mtime REAL, size INTEGER,
  turns INTEGER DEFAULT 0, files_json TEXT DEFAULT '[]',
  tokens_raw INTEGER DEFAULT 0, tokens_compact INTEGER DEFAULT 0,
  first_message TEXT, native_title TEXT,
  parent_instance_id TEXT, from_agent TEXT, moved_at TEXT,
  openable INTEGER DEFAULT 1, extra_json TEXT DEFAULT '{}',
  present INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS instances_conv ON instances(conversation_id);
CREATE INDEX IF NOT EXISTS instances_session ON instances(agent, session_id);
CREATE TABLE IF NOT EXISTS turns (
  instance_id TEXT, n INTEGER, author TEXT, ts TEXT, content TEXT,
  PRIMARY KEY (instance_id, n)
);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
  content, instance_id UNINDEXED, n UNINDEXED, tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS lineage (
  child_instance_id TEXT PRIMARY KEY, parent_instance_id TEXT,
  from_agent TEXT, to_agent TEXT, moved_at TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS issues (
  agent TEXT, path TEXT, field TEXT, message TEXT, seen_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class InstanceRow:
    id: str
    conversation_id: str
    agent: str
    flavor: str
    session_id: str
    path: str
    paths: list[str]
    cwd: str
    roots: list[str]
    started_at: str
    updated_at: str
    mtime: float
    size: int
    turns: int
    files: list[str]
    tokens_raw: int
    tokens_compact: int
    first_message: str
    native_title: str | None
    parent_instance_id: str | None
    from_agent: str | None
    moved_at: str | None
    openable: bool
    extra: dict

    @property
    def is_moved(self) -> bool:
        return bool(self.parent_instance_id)


@dataclass
class ConversationRow:
    id: str
    created_at: str
    updated_at: str
    title: str
    title_source: str
    tags: list[str]
    excluded: bool
    instances: list[InstanceRow] = field(default_factory=list)
    prefs: dict = field(default_factory=dict)
    n: int = 0  # 1-based position in the last listing (CLI handle)

    @property
    def newest(self) -> InstanceRow:
        return self.instances[0]

    @property
    def agents(self) -> list[str]:
        seen: list[str] = []
        for i in self.instances:
            if i.agent not in seen:
                seen.append(i.agent)
        return seen

    @property
    def folder(self) -> str:
        return self.newest.cwd

    @property
    def has_title(self) -> bool:
        return self.title_source in ("native", "override", "generated")


@dataclass
class SearchHit:
    conversation: ConversationRow
    instance_id: str
    turn: int
    snippet: str
    author: str


@dataclass
class RebuildReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    issues: list[AdapterIssue] = field(default_factory=list)
    store_errors: dict[str, str] = field(default_factory=dict)
    agents: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


class Index:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else INDEX_DB
        ensure_cache_dir()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ #
    def _migrate(self) -> None:
        row = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row and int(row["value"]) == SCHEMA_VERSION:
            return
        if row:
            # Preserve user state (overrides, tags, prefs); drop derived tables.
            keep = self.db.execute(
                "SELECT id, title_override, tag_add_json, tag_remove_json, prefs_json, excluded FROM conversations"
            ).fetchall()
            self.db.executescript("DROP TABLE instances; DROP TABLE turns; DROP TABLE turns_fts; DROP TABLE conversations; DROP TABLE lineage; DROP TABLE issues;")
            self.db.executescript(SCHEMA)
            for k in keep:
                self.db.execute(
                    "INSERT INTO conversations(id, title_override, tag_add_json, tag_remove_json, prefs_json, excluded) VALUES (?,?,?,?,?,?)",
                    (k["id"], k["title_override"], k["tag_add_json"], k["tag_remove_json"], k["prefs_json"], k["excluded"]),
                )
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()

    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    # ------------------------------------------------------------------ #
    # Rebuild
    # ------------------------------------------------------------------ #
    def rebuild(
        self,
        full: bool = False,
        only: list[str] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> RebuildReport:
        t0 = time.time()
        report = RebuildReport()
        disc = adapters.discover(only=only)
        report.issues = disc.issues
        report.store_errors = disc.store_errors
        report.agents = disc.agents
        existing = {
            r["id"]: r
            for r in self.db.execute("SELECT id, mtime, size, paths_json, conversation_id FROM instances").fetchall()
        }
        seen: set[str] = set()
        title_cache = titles.load_cache()
        for inst in disc.instances:
            seen.add(inst.id)
            prev = existing.get(inst.id)
            paths_json = json.dumps([str(p) for p in inst.paths])
            unchanged = (
                prev is not None
                and not full
                and abs(float(prev["mtime"] or 0) - inst.mtime) < 1e-6
                and int(prev["size"] or 0) == inst.size
                and prev["paths_json"] == paths_json
            )
            if unchanged:
                report.unchanged += 1
                self._update_cheap(inst)
                continue
            if on_progress:
                on_progress(f"{inst.agent}: {inst.native_title or inst.session_id[:8]}")
            try:
                ex = adapters.extract(inst)
            except Exception as exc:  # noqa: BLE001
                report.issues.append(AdapterIssue(inst.agent, str(inst.path), "extract", f"{type(exc).__name__}: {exc}"))
                continue
            self._upsert_instance(inst, ex, prev)
            if prev is None:
                report.added += 1
            else:
                report.updated += 1
        # Instances whose files disappeared (archived, deleted, moved).
        for iid, row in existing.items():
            if iid not in seen and (not only or iid.split(":", 1)[0] in only):
                self.db.execute("DELETE FROM turns WHERE instance_id=?", (iid,))
                self.db.execute("DELETE FROM turns_fts WHERE instance_id=?", (iid,))
                self.db.execute("DELETE FROM instances WHERE id=?", (iid,))
                report.removed += 1
        self._resolve_lineage()
        self._refresh_conversations(title_cache)
        self.db.execute("DELETE FROM issues")
        now = _now()
        self.db.executemany(
            "INSERT INTO issues(agent, path, field, message, seen_at) VALUES (?,?,?,?,?)",
            [(i.agent, i.path, i.field, i.message, now) for i in report.issues],
        )
        for agent, err in disc.store_errors.items():
            self.db.execute("INSERT INTO issues(agent, path, field, message, seen_at) VALUES (?,?,?,?,?)", (agent, "", "store", err, now))
        self.set_meta("last_build", now)
        self.db.commit()
        report.seconds = time.time() - t0
        return report

    def _update_cheap(self, inst: Instance) -> None:
        self.db.execute(
            "UPDATE instances SET native_title=COALESCE(?, native_title), updated_at=?, cwd=?, present=1 WHERE id=?",
            (inst.native_title, _iso(inst.updated), inst.cwd, inst.id),
        )

    def _upsert_instance(self, inst: Instance, ex: adapters.Extracted, prev) -> None:
        conv_id = prev["conversation_id"] if prev is not None else inst.id
        self.db.execute(
            """INSERT OR REPLACE INTO instances
            (id, conversation_id, agent, flavor, session_id, path, paths_json, cwd, roots_json,
             started_at, updated_at, mtime, size, turns, files_json, tokens_raw, tokens_compact,
             first_message, native_title, parent_instance_id, from_agent, moved_at, openable, extra_json, present)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    (SELECT parent_instance_id FROM instances WHERE id=?),
                    (SELECT from_agent FROM instances WHERE id=?),
                    (SELECT moved_at FROM instances WHERE id=?), ?, ?, 1)""",
            (
                inst.id, conv_id, inst.agent, inst.flavor, inst.session_id, str(inst.path),
                json.dumps([str(p) for p in inst.paths]), inst.cwd, json.dumps(inst.roots),
                _iso(inst.started), _iso(inst.updated), inst.mtime, inst.size, len(ex.turns),
                json.dumps(ex.files_touched), ex.tokens_raw, ex.tokens_compact,
                inst.first_message or (ex.user_msgs[0] if ex.user_msgs else ""), inst.native_title,
                inst.id, inst.id, inst.id, 1 if inst.openable else 0, json.dumps(inst.extra, default=str),
            ),
        )
        if inst.parent_hint:
            from_agent, src = inst.parent_hint
            self.db.execute(
                "INSERT OR IGNORE INTO lineage(child_instance_id, parent_instance_id, from_agent, to_agent, moved_at, source) VALUES (?,?,?,?,?,?)",
                (inst.id, f"{from_agent}:{src}" if src else None, from_agent, inst.agent, _iso(inst.started), "banner"),
            )
        self.db.execute("DELETE FROM turns WHERE instance_id=?", (inst.id,))
        self.db.execute("DELETE FROM turns_fts WHERE instance_id=?", (inst.id,))
        self.db.executemany(
            "INSERT INTO turns(instance_id, n, author, ts, content) VALUES (?,?,?,?,?)",
            [(inst.id, t.n, t.author, t.ts, t.content) for t in ex.turns],
        )
        self.db.executemany(
            "INSERT INTO turns_fts(content, instance_id, n) VALUES (?,?,?)",
            [(t.content, inst.id, t.n) for t in ex.turns],
        )

    def record_lineage(self, child_instance_id: str, parent_instance_id: str, from_agent: str, to_agent: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO lineage(child_instance_id, parent_instance_id, from_agent, to_agent, moved_at, source) VALUES (?,?,?,?,?,?)",
            (child_instance_id, parent_instance_id, from_agent, to_agent, _now(), "ferry"),
        )
        self._resolve_lineage()
        self._refresh_conversations(titles.load_cache())
        self.db.commit()

    def _resolve_lineage(self) -> None:
        """Attach every child instance to its parent's conversation. Chains converge."""
        rows = self.db.execute("SELECT * FROM lineage").fetchall()
        sessions = {
            (r["agent"], r["session_id"]): r["id"]
            for r in self.db.execute("SELECT id, agent, session_id FROM instances").fetchall()
        }
        for _ in range(6):  # bounded fixpoint over chains
            changed = False
            for r in rows:
                child = r["child_instance_id"]
                parent = r["parent_instance_id"]
                if parent and ":" in parent:
                    agent, sid = parent.split(":", 1)
                    if (agent, sid) not in sessions:
                        # prefix match (banner may carry a short id)
                        cands = [v for (a, s), v in sessions.items() if a == agent and s.startswith(sid)]
                        parent = cands[0] if len(cands) == 1 else None
                if not parent:
                    continue
                prow = self.db.execute("SELECT conversation_id FROM instances WHERE id=?", (parent,)).fetchone()
                crow = self.db.execute("SELECT conversation_id, parent_instance_id FROM instances WHERE id=?", (child,)).fetchone()
                if not prow or not crow:
                    continue
                if crow["conversation_id"] != prow["conversation_id"] or crow["parent_instance_id"] != parent:
                    self.db.execute(
                        "UPDATE instances SET conversation_id=?, parent_instance_id=?, from_agent=?, moved_at=? WHERE id=?",
                        (prow["conversation_id"], parent, r["from_agent"], r["moved_at"], child),
                    )
                    # anything hanging off the child follows it
                    self.db.execute(
                        "UPDATE instances SET conversation_id=? WHERE conversation_id=? AND id!=?",
                        (prow["conversation_id"], crow["conversation_id"], child),
                    )
                    changed = True
            if not changed:
                break

    def _refresh_conversations(self, title_cache: dict) -> None:
        conv_ids = [r["conversation_id"] for r in self.db.execute("SELECT DISTINCT conversation_id FROM instances").fetchall()]
        live = set(conv_ids)
        for cid in conv_ids:
            insts = self.db.execute(
                "SELECT * FROM instances WHERE conversation_id=? ORDER BY updated_at DESC", (cid,)
            ).fetchall()
            crow = self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            override = crow["title_override"] if crow else None
            title, source = self._pick_title(insts, override, title_cache)
            inferred = tagmod.infer_from_instances(
                [(json.loads(i["roots_json"] or "[]"), i["cwd"]) for i in insts],
                self._user_text_sample(cid),
            )
            add = json.loads(crow["tag_add_json"] or "[]") if crow else []
            remove = json.loads(crow["tag_remove_json"] or "[]") if crow else []
            cached = tagmod.cached_tags(cid)
            tags_ = sorted((set(inferred) | set(cached) | set(add)) - set(remove))
            created = min(i["started_at"] for i in insts)
            updated = max(i["updated_at"] for i in insts)
            if crow:
                self.db.execute(
                    "UPDATE conversations SET created_at=?, updated_at=?, title=?, title_source=?, tags_json=? WHERE id=?",
                    (created, updated, title, source, json.dumps(tags_), cid),
                )
            else:
                self.db.execute(
                    "INSERT INTO conversations(id, created_at, updated_at, title, title_source, tags_json) VALUES (?,?,?,?,?,?)",
                    (cid, created, updated, title, source, json.dumps(tags_)),
                )
        # conversations with no instances left: keep user state, they just won't list

    def _user_text_sample(self, conv_id: str) -> str:
        rows = self.db.execute(
            "SELECT t.content FROM turns t JOIN instances i ON i.id=t.instance_id WHERE i.conversation_id=? AND t.author='user' ORDER BY i.started_at, t.n LIMIT 40",
            (conv_id,),
        ).fetchall()
        return "\n".join(r["content"] for r in rows)[:20000]

    @staticmethod
    def _pick_title(insts, override: str | None, cache: dict) -> tuple[str, str]:
        if override:
            return override, "override"
        for i in insts:  # newest first
            if i["native_title"]:
                return i["native_title"], "native"
        for i in insts:
            entry = cache.get(i["session_id"])
            if isinstance(entry, dict) and entry.get("title"):
                return str(entry["title"]), "generated"
        for i in insts[::-1]:  # oldest instance holds the real first message
            if i["first_message"]:
                return titles.fallback_title(i["first_message"]), "fallback"
        return "(untitled session)", "fallback"

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def _row_to_instance(self, r) -> InstanceRow:
        return InstanceRow(
            id=r["id"], conversation_id=r["conversation_id"], agent=r["agent"], flavor=r["flavor"],
            session_id=r["session_id"], path=r["path"], paths=json.loads(r["paths_json"] or "[]"),
            cwd=r["cwd"] or "", roots=json.loads(r["roots_json"] or "[]"),
            started_at=r["started_at"] or "", updated_at=r["updated_at"] or "",
            mtime=float(r["mtime"] or 0), size=int(r["size"] or 0), turns=int(r["turns"] or 0),
            files=json.loads(r["files_json"] or "[]"), tokens_raw=int(r["tokens_raw"] or 0),
            tokens_compact=int(r["tokens_compact"] or 0), first_message=r["first_message"] or "",
            native_title=r["native_title"], parent_instance_id=r["parent_instance_id"],
            from_agent=r["from_agent"], moved_at=r["moved_at"], openable=bool(r["openable"]),
            extra=json.loads(r["extra_json"] or "{}"),
        )

    def _row_to_conversation(self, r, with_instances: bool = True) -> ConversationRow:
        conv = ConversationRow(
            id=r["id"], created_at=r["created_at"] or "", updated_at=r["updated_at"] or "",
            title=r["title"] or "(untitled session)", title_source=r["title_source"] or "fallback",
            tags=json.loads(r["tags_json"] or "[]"), excluded=bool(r["excluded"]),
            prefs=json.loads(r["prefs_json"] or "{}"),
        )
        if with_instances:
            conv.instances = [
                self._row_to_instance(i)
                for i in self.db.execute("SELECT * FROM instances WHERE conversation_id=? ORDER BY updated_at DESC", (r["id"],)).fetchall()
            ]
        return conv

    def conversations(
        self,
        query: str | None = None,
        agent: str | None = None,
        tag: str | None = None,
        include_excluded: bool = False,
        limit: int | None = None,
    ) -> list[ConversationRow]:
        rows = self.db.execute(
            "SELECT c.* FROM conversations c WHERE EXISTS (SELECT 1 FROM instances i WHERE i.conversation_id=c.id) ORDER BY c.updated_at DESC"
        ).fetchall()
        out: list[ConversationRow] = []
        matched_ids: set[str] | None = None
        if query and query.strip():
            matched_ids = self._match_ids(query)
        for r in rows:
            if not include_excluded and r["excluded"]:
                continue
            if matched_ids is not None and r["id"] not in matched_ids:
                continue
            conv = self._row_to_conversation(r)
            if not conv.instances:
                continue
            if agent and agent not in conv.agents:
                continue
            if tag and tag not in conv.tags:
                continue
            conv.n = len(out) + 1
            out.append(conv)
            if limit and len(out) >= limit:
                break
        return out

    def _match_ids(self, query: str) -> set[str]:
        ids: set[str] = set()
        like = f"%{query.strip()}%"
        for r in self.db.execute("SELECT id FROM conversations WHERE title LIKE ? OR tags_json LIKE ?", (like, like)):
            ids.add(r["id"])
        for r in self.db.execute(
            "SELECT DISTINCT conversation_id AS id FROM instances WHERE first_message LIKE ? OR cwd LIKE ?", (like, like)
        ):
            ids.add(r["id"])
        fts = fts_query(query)
        if fts:
            try:
                for r in self.db.execute(
                    "SELECT DISTINCT i.conversation_id AS id FROM turns_fts f JOIN instances i ON i.id=f.instance_id WHERE turns_fts MATCH ? LIMIT 500",
                    (fts,),
                ):
                    ids.add(r["id"])
            except sqlite3.OperationalError:
                pass
        return ids

    def conversation(self, conv_id: str) -> ConversationRow | None:
        r = self.db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        if not r:
            return None
        conv = self._row_to_conversation(r)
        return conv if conv.instances else None

    def instance(self, instance_id: str) -> InstanceRow | None:
        r = self.db.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        return self._row_to_instance(r) if r else None

    def turns(self, instance_id: str, from_turn: int | None = None, to_turn: int | None = None) -> list[Turn]:
        sql = "SELECT n, author, ts, content FROM turns WHERE instance_id=?"
        args: list = [instance_id]
        if from_turn is not None:
            sql += " AND n>=?"
            args.append(from_turn)
        if to_turn is not None:
            sql += " AND n<=?"
            args.append(to_turn)
        sql += " ORDER BY n"
        return [Turn(n=r["n"], author=r["author"], ts=r["ts"], content=r["content"]) for r in self.db.execute(sql, args)]

    def conversation_turns(self, conv: ConversationRow) -> list[tuple[InstanceRow, list[Turn]]]:
        """Oldest instance first, so the preview reads top to bottom."""
        return [(i, self.turns(i.id)) for i in sorted(conv.instances, key=lambda i: i.started_at)]

    def search(self, query: str, agent: str | None = None, since: str | None = None, limit: int = 30) -> list[SearchHit]:
        fts = fts_query(query)
        hits: list[SearchHit] = []
        if not fts:
            return hits
        sql = (
            "SELECT f.instance_id, f.n, snippet(turns_fts, 0, '«', '»', '…', 18) AS snip, t.author, i.conversation_id, i.agent, i.updated_at "
            "FROM turns_fts f JOIN turns t ON t.instance_id=f.instance_id AND t.n=f.n "
            "JOIN instances i ON i.id=f.instance_id WHERE turns_fts MATCH ?"
        )
        args: list = [fts]
        if agent:
            sql += " AND i.agent=?"
            args.append(agent)
        if since:
            sql += " AND i.updated_at>=?"
            args.append(since)
        sql += " ORDER BY rank LIMIT ?"
        args.append(limit * 3)
        try:
            rows = self.db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return hits
        convs: dict[str, ConversationRow] = {}
        for r in rows:
            conv = convs.get(r["conversation_id"])
            if conv is None:
                conv = self.conversation(r["conversation_id"])
                if conv is None or conv.excluded:
                    continue
                convs[conv.id] = conv
            hits.append(SearchHit(conversation=conv, instance_id=r["instance_id"], turn=int(r["n"]), snippet=r["snip"], author=r["author"]))
            if len(hits) >= limit:
                break
        return hits

    def issues(self) -> list[AdapterIssue]:
        return [AdapterIssue(r["agent"], r["path"], r["field"], r["message"]) for r in self.db.execute("SELECT * FROM issues ORDER BY agent, path")]

    def stats(self) -> dict:
        n_conv = self.db.execute("SELECT COUNT(*) FROM conversations c WHERE EXISTS (SELECT 1 FROM instances i WHERE i.conversation_id=c.id)").fetchone()[0]
        per_agent = {r["agent"]: r["c"] for r in self.db.execute("SELECT agent, COUNT(*) c FROM instances GROUP BY agent")}
        n_turns = self.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        return {"conversations": n_conv, "instances": per_agent, "turns": n_turns, "last_build": self.meta("last_build"), "path": str(self.path)}

    # ------------------------------------------------------------------ #
    # Writes that hold user state
    # ------------------------------------------------------------------ #
    def set_title_override(self, conv_id: str, title: str | None) -> None:
        self.db.execute("UPDATE conversations SET title_override=? WHERE id=?", (title or None, conv_id))
        self._refresh_conversations(titles.load_cache())
        self.db.commit()

    def set_generated_title(self, conv_id: str, title: str) -> None:
        self.db.execute("UPDATE conversations SET title=?, title_source='generated' WHERE id=? AND title_source IN ('fallback','generated')", (title, conv_id))
        self.db.commit()

    def set_native_title(self, instance_id: str, title: str) -> None:
        self.db.execute("UPDATE instances SET native_title=? WHERE id=?", (title, instance_id))
        self._refresh_conversations(titles.load_cache())
        self.db.commit()

    def edit_tags(self, conv_id: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> list[str]:
        r = self.db.execute("SELECT tag_add_json, tag_remove_json FROM conversations WHERE id=?", (conv_id,)).fetchone()
        cur_add = set(json.loads(r["tag_add_json"] or "[]")) if r else set()
        cur_rm = set(json.loads(r["tag_remove_json"] or "[]")) if r else set()
        for t in add:
            t = tagmod.normalise(t)
            if t:
                cur_add.add(t)
                cur_rm.discard(t)
        for t in remove:
            t = tagmod.normalise(t)
            if t:
                cur_rm.add(t)
                cur_add.discard(t)
        self.db.execute(
            "UPDATE conversations SET tag_add_json=?, tag_remove_json=? WHERE id=?",
            (json.dumps(sorted(cur_add)), json.dumps(sorted(cur_rm)), conv_id),
        )
        self._refresh_conversations(titles.load_cache())
        self.db.commit()
        conv = self.conversation(conv_id)
        return conv.tags if conv else []

    def set_tags(self, conv_id: str, tags_: Iterable[str]) -> list[str]:
        wanted = {tagmod.normalise(t) for t in tags_ if tagmod.normalise(t)}
        conv = self.conversation(conv_id)
        current = set(conv.tags) if conv else set()
        return self.edit_tags(conv_id, add=wanted - current, remove=current - wanted)

    def set_excluded(self, conv_id: str, excluded: bool) -> None:
        self.db.execute("UPDATE conversations SET excluded=? WHERE id=?", (1 if excluded else 0, conv_id))
        if excluded:
            for r in self.db.execute("SELECT id FROM instances WHERE conversation_id=?", (conv_id,)).fetchall():
                self.db.execute("DELETE FROM turns WHERE instance_id=?", (r["id"],))
                self.db.execute("DELETE FROM turns_fts WHERE instance_id=?", (r["id"],))
                self.db.execute("UPDATE instances SET mtime=0 WHERE id=?", (r["id"],))  # force re-extract if un-excluded
        self.db.commit()

    def set_prefs(self, conv_id: str, **prefs) -> dict:
        r = self.db.execute("SELECT prefs_json FROM conversations WHERE id=?", (conv_id,)).fetchone()
        cur = json.loads(r["prefs_json"] or "{}") if r else {}
        cur.update(prefs)
        self.db.execute("UPDATE conversations SET prefs_json=? WHERE id=?", (json.dumps(cur), conv_id))
        self.db.commit()
        return cur

    def all_tags(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for c in self.conversations():
            for t in c.tags:
                counts[t] = counts.get(t, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # ------------------------------------------------------------------ #
    def resolve(self, handle: str, listing: list[ConversationRow] | None = None) -> ConversationRow:
        """Accept '#3', a conversation id, an instance id, a session id prefix, or a unique title fragment."""
        handle = handle.strip()
        convs = listing if listing is not None else self.conversations()
        if handle.startswith("#") and handle[1:].isdigit():
            n = int(handle[1:])
            if 1 <= n <= len(convs):
                return convs[n - 1]
            raise LookupError(f"no conversation #{n} (list has {len(convs)})")
        if handle.isdigit() and 1 <= int(handle) <= len(convs):
            return convs[int(handle) - 1]
        for c in convs:
            if c.id == handle:
                return c
            for i in c.instances:
                if i.id == handle or i.session_id == handle:
                    return c
        pref = [c for c in convs if any(i.session_id.startswith(handle) for i in c.instances)]
        if len(pref) == 1:
            return pref[0]
        frag = [c for c in convs if handle.lower() in c.title.lower()]
        if len(frag) == 1:
            return frag[0]
        if len(frag) > 1:
            raise LookupError(f"{len(frag)} conversations match {handle!r}: " + "; ".join(c.title for c in frag[:5]))
        raise LookupError(f"nothing matches {handle!r}")


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query: quoted tokens, last one prefix-matched."""
    toks = [t for t in re.split(r"\s+", query.strip()) if t]
    if not toks:
        return ""
    parts = []
    for i, t in enumerate(toks):
        t = t.replace('"', '""')
        parts.append(f'"{t}"' + ("*" if i == len(toks) - 1 and len(t) >= 2 else ""))
    return " ".join(parts)
