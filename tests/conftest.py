"""Fictional stores only. No test reads a developer's conversation archive."""
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

_sandbox = tempfile.TemporaryDirectory(prefix="ferry-tests-")
os.environ["FERRY_CACHE"] = str(Path(_sandbox.name) / "cache")
os.environ["FERRY_OFFLINE"] = "1"
os.environ["FERRY_NO_LAUNCH"] = "1"
for _agent in ("CODEX", "CLAUDE", "COWORK", "OPENCODE"):
    os.environ[f"FERRY_{_agent}_HOME"] = str(Path(_sandbox.name) / _agent.lower())

CODEX_ID = "10000000-0000-4000-8000-000000000001"
CLAUDE_ID = "20000000-0000-4000-8000-000000000001"
STAMP = "2026-01-01T12:00:00Z"


def write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    old = time.time() - 120
    os.utime(path, (old, old))
    return path


def codex_records(cwd, sid=CODEX_ID, source="cli"):
    return [
        {"timestamp": STAMP, "type": "session_meta", "payload": {
            "id": sid, "timestamp": STAMP, "cwd": str(cwd), "source": source, "cli_version": "0.1.0"}},
        {"timestamp": STAMP, "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text":
                "# Files mentioned by the user:\n\n## sample.txt: /tmp/example.txt\n\n## My request:\nBuild a tiny weather forecast CLI."}]}},
        {"timestamp": STAMP, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text":
                "The forecast command is ready. Next, add a Celsius option."}]}},
        {"timestamp": STAMP, "type": "response_item", "payload": {
            "type": "function_call", "name": "apply_patch", "call_id": "example-call", "arguments":
                "*** Add File: weather.py\n+print('sunny')"}},
        {"timestamp": STAMP, "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "example-call", "output": "tool-only-canary"}},
    ]


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ferry import adapters, paths, titles, transfer
    from ferry.index import Index

    homes = {a: tmp_path / a for a in ("codex", "claude", "claude-cowork")}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr(adapters, "agents", lambda: list(homes))
    monkeypatch.setattr(adapters, "home_for", lambda agent: homes[agent])
    monkeypatch.setattr(transfer, "home_for", lambda agent: homes[agent])
    monkeypatch.setattr(adapters, "can_open_handoff", lambda agent: agent in {"codex", "claude"})
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(transfer, "WRITES_MANIFEST", cache / "writes.jsonl")
    monkeypatch.setattr(transfer, "ARCHIVE_DIR", cache / "archive")
    monkeypatch.setattr(titles, "TITLES_CACHE", cache / "titles.json")
    monkeypatch.setattr(paths, "LEGACY_PICK_TITLES", tmp_path / "no-legacy-cache")
    project = tmp_path / "weather"
    project.mkdir()
    codex_path = write_records(homes["codex"] / "sessions/2026/01/01" / f"rollout-example-{CODEX_ID}.jsonl", codex_records(project))
    write_records(homes["codex"] / "session_index.jsonl", [{"id": CODEX_ID, "thread_name": "Tiny weather CLI", "updated_at": STAMP}])
    claude_path = write_records(homes["claude"] / "projects/example" / f"{CLAUDE_ID}.jsonl", [
        {"type": "user", "sessionId": CLAUDE_ID, "uuid": "user-example", "timestamp": STAMP,
         "cwd": str(project), "version": "2.1.0", "entrypoint": "cli",
         "message": {"role": "user", "content": "Please add a parser for temperatures."}},
        {"type": "assistant", "sessionId": CLAUDE_ID, "uuid": "assistant-example", "timestamp": STAMP,
         "cwd": str(project), "version": "2.1.0", "entrypoint": "cli",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "The parser accepts Celsius and Fahrenheit."}]}},
        {"type": "custom-title", "sessionId": CLAUDE_ID, "customTitle": "Temperature parser"},
    ])
    cowork_path = homes["claude-cowork"] / "example-account/example-device/local_example.json"
    write_records(cowork_path, [{"sessionId": "30000000-0000-4000-8000-000000000001", "title": "Example checklist", "createdAt": 1767268800000, "lastActivityAt": 1767268800000}])
    write_records(cowork_path.with_suffix("") / "audit.jsonl", [
        {"type": "user", "timestamp": STAMP, "message": {"content": "Draft a fictional release checklist."}},
        {"type": "assistant", "timestamp": STAMP, "message": {"content": [{"type": "text", "text": "Test, package, publish."}]}},
    ])
    idx = Index(path=cache / "index.db")
    report = idx.rebuild()
    assert not report.store_errors, report.store_errors
    yield {"index": idx, "homes": homes, "project": project, "cache": cache, "codex_path": codex_path, "claude_path": claude_path}
    idx.close()
