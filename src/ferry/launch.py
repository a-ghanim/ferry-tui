"""Enter: open an instance in its native agent, in its own folder.

- Claude Code: `claude --remote-control --resume <id>` from the session's cwd,
  so the session also appears at claude.ai/code and on the phone.
- Codex: native threads open through `codex://threads/<id>`. Threads generated
  by ferry use `codex resume <id>` because the desktop app does not render the
  synthetic legacy transcript as prior chat bubbles.
- OpenCode: `opencode --session <id>` in the cwd.
- Cowork: not openable from outside the desktop app; say so.

Nothing here pre-approves permissions. The receiving agent applies its own
settings and trust prompts as it normally would.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LaunchPlan:
    kind: str  # "exec" (takes over the terminal) | "open" (GUI app) | "none"
    argv: list[str]
    cwd: str
    description: str


def plan(
    agent: str,
    session_id: str,
    cwd: str | None,
    openable: bool = True,
    generated: bool = False,
) -> LaunchPlan:
    cwd = cwd if cwd and Path(cwd).is_dir() else str(Path.home())
    if not openable or agent == "claude-cowork":
        return LaunchPlan("none", [], cwd, "Cowork sessions open only inside the Claude desktop app. Tab to Claude Code to continue it.")
    if agent == "claude":
        claude = shutil.which("claude")
        if not claude:
            return LaunchPlan("none", [], cwd, "claude CLI not found on PATH (install Claude Code)")
        return LaunchPlan("exec", [claude, "--remote-control", "--resume", session_id], cwd, "claude --remote-control --resume")
    if agent == "codex":
        codex = shutil.which("codex")
        if generated:
            if codex:
                return LaunchPlan("exec", [codex, "resume", session_id], cwd, "codex resume")
            return LaunchPlan(
                "none",
                [],
                cwd,
                "this Ferry-generated Codex handoff requires the codex CLI; "
                "the Codex app cannot display its imported transcript",
            )
        if shutil.which("open"):
            return LaunchPlan("open", ["open", f"codex://threads/{session_id}"], cwd, "open codex://threads/…")
        if codex:
            return LaunchPlan("exec", [codex, "resume", session_id], cwd, "codex resume")
        return LaunchPlan("none", [], cwd, "neither the Codex app (codex:// scheme) nor the codex CLI is available")
    if agent == "opencode":
        oc = shutil.which("opencode")
        if not oc:
            return LaunchPlan("none", [], cwd, "opencode not found on PATH")
        return LaunchPlan("exec", [oc, "--session", session_id], cwd, "opencode --session")
    return LaunchPlan("none", [], cwd, f"ferry does not know how to open {agent} sessions yet")


def run(p: LaunchPlan) -> int:
    """Run a plan in the foreground. Returns the exit code (0 for GUI opens)."""
    if p.kind == "none":
        return 1
    if p.kind == "open":
        return subprocess.run(p.argv, cwd=p.cwd).returncode
    try:
        os.chdir(p.cwd)
    except OSError:
        pass
    return subprocess.run(p.argv, cwd=p.cwd).returncode


def exec_replace(p: LaunchPlan) -> None:
    """Replace the current process (CLI path). Never returns on success."""
    if p.kind != "exec":
        raise SystemExit(run(p))
    os.chdir(p.cwd)
    os.execv(p.argv[0], p.argv)
