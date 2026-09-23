from __future__ import annotations

from ferry import launch


def fake_which(paths: dict[str, str]):
    return lambda command: paths.get(command)


def test_native_codex_prefers_desktop_app(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", fake_which({"open": "/usr/bin/open", "codex": "/bin/codex"}))

    result = launch.plan("codex", "thread-id", str(tmp_path))

    assert result.kind == "open"
    assert result.argv == ["open", "codex://threads/thread-id"]


def test_generated_codex_requires_cli_and_never_opens_desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", fake_which({"open": "/usr/bin/open", "codex": "/bin/codex"}))

    result = launch.plan("codex", "thread-id", str(tmp_path), generated=True)

    assert result.kind == "exec"
    assert result.argv == ["/bin/codex", "resume", "thread-id"]


def test_generated_codex_without_cli_explains_gui_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", fake_which({"open": "/usr/bin/open"}))

    result = launch.plan("codex", "thread-id", str(tmp_path), generated=True)

    assert result.kind == "none"
    assert result.argv == []
    assert "requires the codex CLI" in result.description
    assert "cannot display" in result.description


def test_native_codex_falls_back_to_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", fake_which({"codex": "/bin/codex"}))

    result = launch.plan("codex", "thread-id", str(tmp_path))

    assert result.kind == "exec"
    assert result.argv == ["/bin/codex", "resume", "thread-id"]
