"""Record demo/ferry.gif by driving the real TUI headlessly.

This script uses Textual's own renderer with entirely fictional conversations:

    named list → cursor moves → search → Tab → linked context in the other
    agent's store (a real transfer, undone at the end; agent launch disabled).

Usage:  .venv/bin/python demo/make_gif.py [--keep]
Needs:  ffmpeg on PATH, macOS `qlmanage` (SVG → PNG). Frames land in
        demo/_frames/ and are deleted unless --keep.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FRAMES = HERE / "_frames"
OUT = HERE / "ferry.gif"
SIZE = (150, 42)
DEMO_HOME = Path(tempfile.mkdtemp(prefix="ferry-demo-"))
DEMO_PROJECT = Path("/tmp") / f"ferry-demo-project-{os.getpid()}"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _codex_session(
    home: Path,
    session_id: str,
    timestamp: str,
    title: str,
    user: str,
    assistant: str,
    position: int,
) -> None:
    path = home / "sessions" / "2026" / "09" / "18" / f"rollout-{timestamp[:10]}-{session_id}.jsonl"
    cwd = f"/Users/demo/Projects/{title.lower().replace(' ', '-')}"
    records = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": timestamp,
                "cwd": cwd,
                "originator": "codex_cli_rs",
                "cli_version": "0.1.0",
                "source": "cli",
            },
        },
        {"timestamp": timestamp, "type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-5"}},
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": user}]},
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": assistant}]},
        },
    ]
    _write_jsonl(path, records)
    settled = time.time() - 600 + position
    os.utime(path, (settled, settled))
    with (home / "session_index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": session_id, "thread_name": title, "updated_at": timestamp}) + "\n")


def _claude_session(
    home: Path,
    session_id: str,
    timestamp: str,
    title: str,
    user: str,
    assistant: str,
) -> None:
    cwd = f"/Users/demo/Projects/{title.lower().replace(' ', '-')}"
    project = home / "projects" / ("-" + cwd.strip("/").replace("/", "-"))
    path = project / f"{session_id}.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": f"{session_id}-user",
            "timestamp": timestamp,
            "cwd": cwd,
            "version": "2.1.0",
            "entrypoint": "cli",
            "message": {"role": "user", "content": user},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "uuid": f"{session_id}-assistant",
            "timestamp": timestamp,
            "cwd": cwd,
            "version": "2.1.0",
            "entrypoint": "cli",
            "message": {"role": "assistant", "model": "claude-sonnet", "content": [{"type": "text", "text": assistant}]},
        },
        {"type": "ai-title", "sessionId": session_id, "aiTitle": title},
    ]
    _write_jsonl(path, records)
    settled = time.time() - 900
    os.utime(path, (settled, settled))


def prepare_demo_home() -> None:
    """Build a public, entirely synthetic archive. Never copy user or test stores."""
    codex = DEMO_HOME / "codex-home"
    claude = DEMO_HOME / "claude-home"
    cowork = DEMO_HOME / "cowork"
    opencode = DEMO_HOME / "opencode"
    for destination in (codex / "sessions", claude / "projects", cowork, opencode):
        destination.mkdir(parents=True, exist_ok=True)
    (codex / "session_index.jsonl").touch()

    _codex_session(
        codex,
        "10000000-0000-4000-8000-000000000001",
        "2026-09-18T16:30:00Z",
        "Plan a tiny launch checklist",
        "Help me turn the release notes into a tiny launch checklist.",
        "I made a three-step checklist: test the package, publish the release, then share the demo.",
        4,
    )
    _codex_session(
        codex,
        "10000000-0000-4000-8000-000000000002",
        "2026-09-18T15:10:00Z",
        "Add keyboard shortcuts to notes",
        "Add a shortcut to jump between the note list and preview.",
        "The shortcut is wired up and documented in the footer.",
        2,
    )
    _codex_session(
        codex,
        "10000000-0000-4000-8000-000000000003",
        "2026-09-18T13:05:00Z",
        "Sketch a calmer empty state",
        "Can you sketch a calmer empty state for the app?",
        "Yes — one sentence, one action, and no decorative card around it.",
        0,
    )
    _claude_session(
        claude,
        "20000000-0000-4000-8000-000000000001",
        "2026-09-18T14:20:00Z",
        "Refactor the parser tests",
        "Please make the parser tests easier to read without changing behavior.",
        "I grouped the fixtures by format and kept the assertions identical.",
    )
    _claude_session(
        claude,
        "20000000-0000-4000-8000-000000000002",
        "2026-09-18T12:15:00Z",
        "Name the settings sections",
        "Give these settings sections short, plain-language names.",
        "I renamed them General, Appearance, Connections, and Privacy.",
    )

    os.environ["FERRY_CODEX_HOME"] = str(codex)
    os.environ["FERRY_CLAUDE_HOME"] = str(claude)
    os.environ["FERRY_COWORK_HOME"] = str(cowork)
    os.environ["FERRY_OPENCODE_HOME"] = str(opencode)
    os.environ["FERRY_CACHE"] = str(DEMO_HOME / "cache")
    os.environ["FERRY_NO_LAUNCH"] = "1"
    os.environ["FERRY_OFFLINE"] = "1"
    DEMO_PROJECT.mkdir(parents=True)


prepare_demo_home()

sys.path.insert(0, str(ROOT / "src"))

from ferry import transfer  # noqa: E402
from ferry.tui import FerryApp, TabDialog  # noqa: E402
from textual.widgets import Input  # noqa: E402

DEMO_QUERY = os.environ.get("FERRY_DEMO_QUERY", "launch")


async def record(frames: list[tuple[str, float]]) -> None:
    app = FerryApp()
    n = 0

    def shot(seconds: float) -> None:
        nonlocal n
        n += 1
        path = FRAMES / f"f{n:03d}.svg"
        app.save_screenshot(str(path))
        frames.append((str(path), seconds))

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(1.2)
        shot(1.6)
        for _ in range(3):
            await pilot.press("down")
            await pilot.pause(0.15)
            shot(0.35)
        await pilot.press("slash")
        await pilot.pause(0.1)
        shot(0.4)
        typed = ""
        for ch in DEMO_QUERY:
            typed += ch
            await pilot.press(ch)
            await pilot.pause(0.25)
            shot(0.22)
        await pilot.pause(0.4)
        shot(1.2)
        await pilot.press("enter")  # back to the list, first match selected
        await pilot.pause(0.3)
        shot(0.8)
        await pilot.press("tab")
        await pilot.pause(0.5)
        if not isinstance(app.screen, TabDialog):
            app.action_move()
            await pilot.pause(0.5)
        if not isinstance(app.screen, TabDialog):
            raise RuntimeError("Tab did not open the handoff dialog")
        dialog = app.screen
        dialog.query_one("#folders", Input).value = str(DEMO_PROJECT)
        shot(2.2)
        # confirm the move (real hand-off; undone below)
        dialog.action_go()
        for _ in range(40):  # wait for the worker
            await pilot.pause(0.25)
            if app.index.conversations(query=DEMO_QUERY) and len(app.index.conversations(query=DEMO_QUERY)[0].instances) > 1:
                break
        else:
            raise RuntimeError("handoff did not complete; refusing to record a success frame")
        await pilot.pause(0.6)
        shot(1.6)
        await pilot.press("e")
        await pilot.pause(0.4)
        shot(1.2)
        await pilot.press("down")
        await pilot.pause(0.4)
        shot(2.4)
        await pilot.press("q")


def svg_to_png(svg: str) -> str:
    out_dir = Path(svg).parent
    subprocess.run(["qlmanage", "-t", "-s", "1500", "-o", str(out_dir), svg], capture_output=True)
    png = Path(svg + ".png")
    if not png.exists():
        raise SystemExit(f"qlmanage did not produce {png}")
    # Quick Look always returns a square thumbnail, padding wide terminal
    # screenshots with white above and below. Restore the SVG's true ratio.
    source = Path(svg).read_text(encoding="utf-8")[:2000]
    match = re.search(r'viewBox="([^\"]+)"', source)
    if match:
        _, _, width, height = (float(value) for value in match.group(1).split())
        cropped = png.with_name(png.stem + "-cropped.png")
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(png), "-vf", f"crop=iw:iw/{width / height:.8f}", str(cropped)],
            check=True,
        )
        cropped.replace(png)
    return str(png)


def assert_public_frames(frames: list[tuple[str, float]]) -> None:
    """Fail closed if a capture ever contains a real local path or old fixture."""
    forbidden = (str(Path.home()),)
    for svg, _ in frames:
        content = Path(svg).read_text(encoding="utf-8", errors="replace")
        leaked = next((marker for marker in forbidden if marker and marker in content), None)
        if leaked:
            raise RuntimeError(f"refusing to publish demo frame containing private marker: {leaked!r}")


def assemble(frames: list[tuple[str, float]]) -> None:
    concat = FRAMES / "frames.txt"
    lines = []
    pngs = [(svg_to_png(svg), d) for svg, d in frames]
    for png, d in pngs:
        lines.append(f"file '{png}'")
        lines.append(f"duration {d}")
    lines.append(f"file '{pngs[-1][0]}'")
    concat.write_text("\n".join(lines) + "\n")
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-vf", "fps=12,scale=1400:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3",
            str(OUT),
        ],
        check=True,
    )


def main() -> None:
    keep = "--keep" in sys.argv
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not on PATH (brew install ffmpeg)")
    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True)
    frames: list[tuple[str, float]] = []
    try:
        asyncio.run(record(frames))
        assert_public_frames(frames)
        assemble(frames)
        print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(frames)} frames)")
    finally:
        from ferry.index import Index

        print("undo:", transfer.undo(Index()))
        if not keep and FRAMES.exists():
            shutil.rmtree(FRAMES)
        shutil.rmtree(DEMO_HOME, ignore_errors=True)
        shutil.rmtree(DEMO_PROJECT, ignore_errors=True)


if __name__ == "__main__":
    main()
