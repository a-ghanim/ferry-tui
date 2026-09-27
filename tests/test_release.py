"""Release behavior tested with invented transcripts, including real adapter writes."""
import hashlib
import json
import os
import time

import pytest

from conftest import CODEX_ID, CLAUDE_ID, codex_records, write_records
from ferry import adapters, launch, transfer
from ferry.cleaning import clean_user_text, handoff_lineage
from ferry.index import Index


def pick(env, agent="codex"):
    sid = CODEX_ID if agent == "codex" else CLAUDE_ID
    inst = env["index"].instance(f"{agent}:{sid}")
    return env["index"].conversation(inst.conversation_id), inst


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_index_search_and_incremental_rebuild(env):
    idx = env["index"]
    assert idx.stats()["instances"] == {"codex": 1, "claude": 1, "claude-cowork": 1}
    assert len(idx.conversations()) == 3
    assert idx.search("forecast")[0].instance_id == f"codex:{CODEX_ID}"
    assert not idx.search("tool-only-canary")
    assert not idx.search("recommended_plugins")
    conv, inst = pick(env)
    assert conv.title == "Tiny weather CLI"
    assert inst.first_message == "Build a tiny weather forecast CLI."
    assert inst.files == ["weather.py"]
    assert not idx.rebuild().changed
    assert idx.rebuild(full=True).updated == 3
    assert os.stat(idx.path).st_mode & 0o777 == 0o600


def test_exclude_removes_searchable_text_and_unexclude_restores(env):
    idx = env["index"]
    conv, inst = pick(env)
    idx.set_excluded(conv.id, True)
    assert not idx.search("forecast")
    assert not idx.turns(inst.id)
    idx.rebuild(full=True)
    assert not idx.search("forecast")
    idx.set_excluded(conv.id, False)
    idx.rebuild()
    assert idx.search("forecast")


def test_tags_and_title_survive_rebuild(env):
    idx = env["index"]
    conv, _ = pick(env)
    idx.set_tags(conv.id, ["Demo", "weather"])
    idx.set_title_override(conv.id, "Forecast project")
    idx.rebuild(full=True)
    assert idx.conversation(conv.id).title == "Forecast project"
    assert idx.conversation(conv.id).tags == ["demo", "weather"]
    assert idx.resolve("Forecast project").id == conv.id


@pytest.mark.parametrize("source,target", [("codex", "claude"), ("claude", "codex")])
@pytest.mark.parametrize("mode", ["raw", "compact", "brief"])
def test_transfer_preserves_source_roundtrips_links_and_undoes(env, source, target, mode):
    idx = env["index"]
    conv, inst = pick(env, source)
    original = env[f"{source}_path"]
    before = digest(original)
    result = transfer.move(idx, conv, inst, target, mode=mode, folders=[str(env["project"])])
    assert result.path.exists()
    assert digest(original) == before
    generated = idx.instance(result.new_instance_id)
    assert generated.is_moved and generated.parent_instance_id == inst.id
    assert generated.conversation_id == conv.id
    assert len(idx.conversations()) == 3
    assert len(idx.conversation(conv.id).instances) == 2
    assert idx.turns(generated.id)
    content = result.path.read_text()
    assert "recommended_plugins" not in content
    assert "## My request" not in content
    if mode == "compact":
        assert "tool-only-canary" not in content
    assert transfer.manifest()[-1].session_id == result.new_session_id
    assert "undid hand-off" in transfer.undo(idx)
    assert not result.path.exists()
    assert idx.instance(generated.id) is None
    assert digest(original) == before
    assert list((env["cache"] / "archive").rglob(result.path.name))


def test_live_session_refused_before_any_write(env):
    conv, inst = pick(env)
    os.utime(env["codex_path"], (time.time(), time.time()))
    with pytest.raises(transfer.LiveSessionError):
        transfer.move(env["index"], conv, inst, "claude", folders=[str(env["project"])])
    assert not transfer.manifest()


def test_bad_target_folder_refused(env):
    conv, inst = pick(env)
    with pytest.raises(transfer.TransferError, match="does not exist"):
        transfer.move(env["index"], conv, inst, "claude", folders=[str(env["project"] / "missing")])
    assert not transfer.manifest()


def test_failed_roundtrip_quarantines_generated_file(env, monkeypatch):
    conv, inst = pick(env)
    before = digest(env["codex_path"])
    def reject(*args):
        raise ValueError("synthetic validation failure")
    monkeypatch.setattr(transfer, "_validate_roundtrip", reject)
    with pytest.raises(transfer.TransferError, match="refused to hand over"):
        transfer.move(env["index"], conv, inst, "claude", folders=[str(env["project"])])
    assert transfer.manifest()[-1].undone
    assert list((env["cache"] / "archive/failed").rglob("*.jsonl"))
    assert digest(env["codex_path"]) == before


def test_archive_and_restore(env):
    conv, inst = pick(env)
    before = digest(env["codex_path"])
    transfer.archive(env["index"], conv, inst)
    assert not env["codex_path"].exists()
    assert "restored" in transfer.undo(env["index"])
    assert digest(env["codex_path"]) == before


def test_cowork_is_readonly(env):
    idx = env["index"]
    conv = next(c for c in idx.conversations() if "claude-cowork" in c.agents)
    assert idx.turns(conv.newest.id)
    assert not conv.newest.openable
    assert not adapters.can_inject("claude-cowork")
    source, inst = pick(env)
    with pytest.raises(transfer.TransferError, match="cannot be created"):
        transfer.move(idx, source, inst, "claude-cowork")


def test_subagents_filtered_and_rollouts_collapsed(env):
    home = env["homes"]["codex"]
    write_records(home / "sessions/2026/01/02/rollout-second.jsonl", codex_records(env["project"]))
    write_records(home / "sessions/2026/01/02/rollout-review.jsonl", codex_records(env["project"], "90000000-0000-4000-8000-000000000009", {"subagent": {"other": "review"}}))
    discovered = adapters.discover(only=["codex"])
    assert len(discovered.instances) == 1
    assert len(discovered.instances[0].paths) == 2
    assert not discovered.issues


def test_truncated_record_tolerated_and_unknown_shape_reported(env):
    path = env["codex_path"]
    with path.open("a") as stream:
        stream.write('{"unfinished":')
    assert adapters.discover(only=["codex"]).instances
    write_records(env["homes"]["codex"] / "sessions/2026/01/01/rollout-bad.jsonl", [{"type": "unknown"}])
    report = adapters.discover(only=["codex"])
    assert report.issues[0].field == "type"


@pytest.mark.parametrize("tag", ["recommended_plugins", "environment_context", "app-context", "skills", "permissions", "system-reminder", "command-name"])
def test_harness_only_messages_are_hidden(tag):
    assert clean_user_text(f"<{tag}>generated wrapper</{tag}>") == ("", "")


def test_embedded_harness_and_lineage():
    assert clean_user_text("<system-reminder>metadata</system-reminder>\nBuild the example.") == ("Build the example.", "")
    assert handoff_lineage(f"[handoff] Context transferred from codex.\n- Session: `{CODEX_ID}`") == ("codex", CODEX_ID)


def test_offline_never_invokes_claude(monkeypatch):
    from ferry import titles
    def forbidden(*args, **kwargs):
        pytest.fail("offline mode invoked a subprocess")
    monkeypatch.setattr(titles.subprocess, "run", forbidden)
    assert titles.run_claude_p("fictional request") is None


def test_custom_cache_does_not_import_legacy_titles(env, monkeypatch):
    from ferry import paths
    legacy = env["cache"] / "legacy.json"
    legacy.write_text('{"private-title": "must not migrate"}')
    destination = env["cache"] / "fresh-titles.json"
    monkeypatch.setattr(paths, "LEGACY_PICK_TITLES", legacy)
    monkeypatch.setattr(paths, "TITLES_CACHE", destination)
    paths.ensure_cache_dir()
    assert not destination.exists()


def test_mcp_uses_synthetic_archive_and_labels_transcripts(env, monkeypatch):
    from ferry import mcp_server as m
    monkeypatch.setattr(m, "_index", env["index"])
    results = m.search_conversations("forecast")
    assert results["count"] > 0
    assert results["notice"] == m.NOTICE
    conv, _ = pick(env)
    response = m.get_conversation(conv.id)
    assert response["notice"] == m.NOTICE
    assert response["conversation"]["turns"]
    assert m.get_conversation(conv.id, from_turn=0, to_turn=0)["conversation"]["turns"][0]["n"] == 0
    assert m.search_conversations("zzzz-no-match")["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(150, 42), (80, 24)])
async def test_tui_search_and_preview(env, monkeypatch, size):
    from ferry.tui import FerryApp
    from textual.widgets import DataTable, Input
    import ferry.tui as tui
    monkeypatch.setattr(tui, "Index", lambda: env["index"])
    app = FerryApp()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.query_one("#search", Input).value = "forecast"
        await pilot.pause(0.5)
        assert len(app.index.conversations(query="forecast")) == 1
        assert app.screen.has_class("narrow") == (size[0] < 100)
        assert app.query_one("#list", DataTable).row_count == 1
        if size[0] < 100:
            await pilot.press("p")
            await pilot.pause()
            assert app.screen.has_class("previewing")


@pytest.mark.asyncio
async def test_import_preview_keeps_context_when_source_not_shown(env, monkeypatch):
    from ferry.tui import FerryApp
    from textual.widgets import Static
    from rich.console import Console
    import ferry.tui as tui
    conv, inst = pick(env)
    result = transfer.move(env["index"], conv, inst, "claude", mode="compact", folders=[str(env["project"])])
    generated = env["index"].instance(result.new_instance_id)
    conv = env["index"].conversation(conv.id)
    monkeypatch.setattr(tui, "Index", lambda: env["index"])
    app = FerryApp()
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause()
        app.rows = [("inst", conv, generated)]
        app._show_preview(0)
        console = Console(record=True, width=120)
        with console.capture() as capture:
            console.print(app.query_one("#preview-body", Static).content)
        text = capture.get()
        assert "2 source turns" in text
        assert "Celsius" in text
