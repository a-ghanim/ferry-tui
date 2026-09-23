"""ferry command line. `ferry` alone opens the TUI; everything else is a small command over the index."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import click

from ferry import __version__, adapters, titles
from ferry.adapters import agent_icon, agent_label
from ferry.index import ConversationRow, Index
from ferry.paths import CACHE_DIR, INDEX_DB, display_path


def _local(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def _date(ts: str) -> str:
    return _local(ts)[:10]


def _open_index(refresh: bool = True, quiet: bool = False) -> Index:
    idx = Index()
    if refresh:
        report = idx.rebuild()
        if not quiet:
            for agent, err in report.store_errors.items():
                click.echo(click.style(f"✗ {agent}: {err}", fg="red"), err=True)
            if report.issues:
                click.echo(click.style(f"! {len(report.issues)} file(s) could not be parsed — see `ferry doctor`", fg="yellow"), err=True)
    return idx


def _print_conv(c: ConversationRow, verbose: bool) -> None:
    icons = "".join(agent_icon(a) for a in c.agents)
    tags = ("  #" + " #".join(c.tags)) if c.tags else ""
    mark = "" if c.has_title else "  ⚠ no title yet"
    click.echo(f"{c.n:>3}  {_date(c.updated_at)}  {icons:<3} {c.title[:56]:<56}  {display_path(c.folder)}{tags}{mark}")
    if verbose:
        first = c.instances[-1].first_message or "(no user message found)"
        click.echo(f"                 ↳ {first[:110]}")
        if len(c.instances) > 1:
            for i in c.instances:
                via = f" ← {agent_label(i.from_agent)}" if i.from_agent else ""
                click.echo(f"                   · {agent_label(i.agent)} {_local(i.updated_at)} {i.turns} turns{via}")


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="ferry")
@click.pass_context
def main(ctx: click.Context) -> None:
    """One window for every AI coding conversation on this machine."""
    if ctx.invoked_subcommand is None:
        from ferry.tui import run_tui

        run_tui()


@main.command("index")
@click.option("--full", is_flag=True, help="re-extract every session, ignoring mtimes")
@click.option("--agent", "agent", default=None, help="only this agent")
def index_cmd(full: bool, agent: str | None) -> None:
    """Rebuild the archive (incremental by default)."""
    idx = Index()
    report = idx.rebuild(full=full, only=[agent] if agent else None, on_progress=lambda s: click.echo(f"  … {s}", err=True))
    st = idx.stats()
    click.echo(f"agents: {', '.join(report.agents)}")
    click.echo(f"+{report.added} ~{report.updated} -{report.removed} ={report.unchanged} in {report.seconds:.1f}s → {st['conversations']} conversations, {st['turns']} turns")
    for agent_, err in report.store_errors.items():
        click.echo(click.style(f"✗ {agent_}: {err}", fg="red"))
    for issue in report.issues[:20]:
        click.echo(click.style(f"! {issue}", fg="yellow"))
    if len(report.issues) > 20:
        click.echo(click.style(f"! … and {len(report.issues) - 20} more (ferry doctor)", fg="yellow"))


@main.command("list")
@click.option("--agent", default=None)
@click.option("--tag", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.option("-v", "--verbose", is_flag=True, help="show first message and instances")
@click.option("--no-refresh", is_flag=True)
def list_cmd(agent: str | None, tag: str | None, as_json: bool, verbose: bool, no_refresh: bool) -> None:
    """List conversations, newest first. Handles (#n) work with the other commands."""
    idx = _open_index(refresh=not no_refresh)
    convs = idx.conversations(agent=agent, tag=tag)
    if as_json:
        click.echo(json.dumps([_conv_json(c) for c in convs], indent=2, ensure_ascii=False))
        return
    if not convs:
        click.echo(_empty_message(idx))
        return
    for c in convs:
        _print_conv(c, verbose)
    titles.warn_once_if_unavailable()


def _conv_json(c: ConversationRow) -> dict:
    return {
        "n": c.n,
        "id": c.id,
        "title": c.title,
        "title_source": c.title_source,
        "tags": c.tags,
        "updated": c.updated_at,
        "created": c.created_at,
        "folder": c.folder,
        "agents": c.agents,
        "instances": [
            {
                "id": i.id, "agent": i.agent, "flavor": i.flavor, "turns": i.turns, "updated": i.updated_at,
                "cwd": i.cwd, "roots": i.roots, "files_touched": i.files, "tokens": i.tokens_raw,
                "moved_from": i.from_agent, "first_message": i.first_message,
            }
            for i in c.instances
        ],
    }


def _empty_message(idx: Index) -> str:
    issues = idx.issues()
    stores = [i for i in issues if i.field == "store"]
    if stores:
        return "\n".join(f"{i.message}" for i in stores)
    return "No sessions found. Stores checked: " + ", ".join(f"{a} ({display_path(adapters.home_for(a))})" for a in adapters.agents())


@main.command("search")
@click.argument("query", nargs=-1, required=True)
@click.option("--agent", default=None)
@click.option("--since", default=None, help="ISO date")
@click.option("--limit", default=20)
def search_cmd(query: tuple[str, ...], agent: str | None, since: str | None, limit: int) -> None:
    """Full-text search over titles and message content."""
    idx = _open_index(quiet=True)
    q = " ".join(query)
    hits = idx.search(q, agent=agent, since=since, limit=limit)
    if not hits:
        click.echo("no matches")
        return
    for h in hits:
        c = h.conversation
        who = "you" if h.author == "user" else agent_label(h.instance_id.split(":", 1)[0]).split()[0].lower()
        click.echo(f"{_date(c.updated_at)}  {c.title[:50]:<50}  {who} › {h.snippet.replace(chr(10), ' ')[:110]}")


@main.command("show")
@click.argument("handle")
@click.option("--turns", "turn_range", default=None, help="e.g. 0-20")
def show_cmd(handle: str, turn_range: str | None) -> None:
    """Print a conversation as readable prose."""
    idx = _open_index(quiet=True)
    conv = _resolve(idx, handle)
    lo = hi = None
    if turn_range:
        a, _, b = turn_range.partition("-")
        lo = int(a) if a else None
        hi = int(b) if b else None
    click.echo(click.style(conv.title, bold=True))
    click.echo(click.style(f"{_local(conv.updated_at)} · {display_path(conv.folder)} · " + (" ".join('#' + t for t in conv.tags) or "no tags"), dim=True))
    for inst, turns in idx.conversation_turns(conv):
        header = f"— {agent_label(inst.agent)} · {inst.turns} turns"
        if inst.from_agent:
            header += f" · moved from {agent_label(inst.from_agent)}"
        click.echo(click.style(header, dim=True))
        for t in turns:
            if lo is not None and t.n < lo or hi is not None and t.n > hi:
                continue
            who = "you ›" if t.author == "user" else f"{agent_label(inst.agent).split()[0].lower()} ›"
            click.echo("")
            click.echo(click.style(who, dim=True) + " " + t.content)


@main.command("open")
@click.argument("handle")
@click.option("--instance", "which", default=None, help="agent name to open when the conversation spans several")
def open_cmd(handle: str, which: str | None) -> None:
    """Enter: open in its native agent, in its own folder."""
    from ferry import launch

    idx = _open_index(quiet=True)
    conv = _resolve(idx, handle)
    inst = _choose_instance(conv, which)
    p = launch.plan(inst.agent, inst.session_id, inst.cwd, inst.openable, generated=inst.is_moved)
    click.echo(f"→ {conv.title}", err=True)
    click.echo(f"  {agent_label(inst.agent)} · {display_path(p.cwd)} · {p.description}", err=True)
    if p.kind == "none":
        raise SystemExit(1)
    launch.exec_replace(p)


@main.command("tab")
@click.argument("handle")
@click.option("--to", "to_agent", default=None, help="target agent (default: the other one)")
@click.option("--mode", default="auto", type=click.Choice(["auto", "raw", "compact", "brief"]))
@click.option("--folder", "folders", multiple=True, help="folder(s) the receiving agent should have (default: previous roots)")
@click.option("--no-open", is_flag=True, help="move but do not open")
@click.option("--force-live", is_flag=True, hidden=True)
def tab_cmd(handle: str, to_agent: str | None, mode: str, folders: tuple[str, ...], no_open: bool, force_live: bool) -> None:
    """Tab: hand the conversation to the other agent and open it there."""
    from ferry import launch, transfer

    idx = _open_index(quiet=True)
    conv = _resolve(idx, handle)
    inst = conv.newest
    targets = transfer.targets_for(inst)
    if not targets:
        raise click.ClickException("no other agent can receive this conversation")
    if to_agent is None:
        if len(targets) == 1:
            to_agent = targets[0]
        else:
            raise click.ClickException("several targets available: " + ", ".join(targets) + " — pass --to")
    click.echo(f"→ {conv.title}", err=True)
    click.echo(f"  {transfer.summary_line(inst)}", err=True)
    try:
        res = transfer.move(idx, conv, inst, to_agent, mode=mode, folders=list(folders) or (conv.prefs.get("folders") or None), force_live=force_live)
    except transfer.TransferError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"  ✓ {res.banner} → {agent_label(to_agent)} · folders: {', '.join(display_path(f) for f in res.folders)}", err=True)
    if no_open:
        click.echo(f"  open it with: ferry open {conv.n or repr(conv.title)} --instance {to_agent}", err=True)
        return
    p = launch.plan(to_agent, res.new_session_id, res.folders[0], generated=True)
    if p.kind == "none":
        raise click.ClickException(p.description)
    launch.exec_replace(p)


@main.command("fork")
@click.argument("handle")
@click.option("--mode", default="auto", type=click.Choice(["auto", "raw", "compact", "brief"]))
def fork_cmd(handle: str, mode: str) -> None:
    """Second opinion: fork the conversation to every other agent and open them side by side."""
    from ferry import launch, transfer

    idx = _open_index(quiet=True)
    conv = _resolve(idx, handle)
    inst = conv.newest
    try:
        results = transfer.second_opinion(idx, conv, inst, mode=mode)
    except transfer.TransferError as exc:
        raise click.ClickException(str(exc))
    plans = [launch.plan(inst.agent, inst.session_id, inst.cwd, inst.openable, generated=inst.is_moved)]
    for r in results:
        click.echo(f"  ✓ {r.banner} → {agent_label(r.to_agent)}", err=True)
        plans.append(launch.plan(r.to_agent, r.new_session_id, r.folders[0], generated=True))
    execs = [p for p in plans if p.kind == "exec"]
    for p in plans:
        if p.kind == "open":
            launch.run(p)
    if execs:
        launch.exec_replace(execs[-1])


@main.command("rename")
@click.argument("handle")
@click.argument("title", nargs=-1, required=True)
def rename_cmd(handle: str, title: tuple[str, ...]) -> None:
    """Rename a conversation (written back to the agent's own store where it has one)."""
    from ferry import transfer

    idx = _open_index(quiet=True)
    conv = _resolve(idx, handle)
    where = transfer.rename(idx, conv, " ".join(title))
    click.echo(f"renamed in {where}")


@main.command("archive")
@click.argument("handle")
def archive_cmd(handle: str) -> None:
    """Move the session file(s) to ~/.cache/ferry/archive/. Never deletes. `ferry undo` restores."""
    from ferry import transfer

    idx = _open_index(quiet=True)
    conv = _resolve(idx, handle)
    for inst in conv.instances:
        try:
            click.echo(f"{agent_label(inst.agent)}: {transfer.archive(idx, conv, inst)}")
        except transfer.TransferError as exc:
            raise click.ClickException(str(exc))


@main.command("undo")
def undo_cmd() -> None:
    """Reverse ferry's last write (hand-off or archive)."""
    from ferry import transfer

    idx = Index()
    click.echo(transfer.undo(idx))


@main.command("tags")
@click.argument("handle", required=False)
@click.argument("edits", nargs=-1)
@click.option("--infer", is_flag=True, help="ask `claude -p` for tags where inference found none (cached)")
def tags_cmd(handle: str | None, edits: tuple[str, ...], infer: bool) -> None:
    """Show or edit tags. `ferry tags` lists all; `ferry tags #3 +weather -misc` edits; `ferry tags --infer` fills gaps."""
    from ferry import tags as tagmod

    idx = _open_index(quiet=True)
    if handle is None:
        if infer:
            n = 0
            for c in idx.conversations():
                if c.tags:
                    continue
                got = tagmod.generate_tags(c.id, idx._user_text_sample(c.id), [t for t, _ in idx.all_tags()])
                if got:
                    n += 1
                    click.echo(f"  {c.title[:50]:<50} → {' '.join('#' + t for t in got)}")
            idx.rebuild()
            click.echo(f"tagged {n} conversation(s)")
            titles.warn_once_if_unavailable()
            return
        for tag, count in idx.all_tags():
            click.echo(f"{count:>4}  #{tag}")
        return
    conv = _resolve(idx, handle)
    add = [e[1:] for e in edits if e.startswith("+")]
    remove = [e[1:] for e in edits if e.startswith("-")]
    plain = [e for e in edits if not e[:1] in "+-"]
    if plain and not add and not remove:
        tags_ = idx.set_tags(conv.id, plain)
    else:
        tags_ = idx.edit_tags(conv.id, add=add, remove=remove)
    if infer and not tags_:
        got = tagmod.generate_tags(conv.id, idx._user_text_sample(conv.id), [t for t, _ in idx.all_tags()])
        if got:
            idx.rebuild()
            tags_ = got
    click.echo(f"{conv.title}: " + (" ".join("#" + t for t in tags_) or "(no tags)"))


@main.command("titles")
@click.option("--generate", is_flag=True, help="generate missing titles with `claude -p` now (otherwise lazy in the TUI)")
def titles_cmd(generate: bool) -> None:
    """Show which conversations lack a title; optionally generate them."""
    idx = _open_index(quiet=True)
    missing = [c for c in idx.conversations() if not c.has_title]
    if not generate:
        click.echo(f"{len(missing)} conversation(s) without a native or generated title")
        for c in missing:
            click.echo(f"  {c.n:>3}  {c.title}")
        return
    for c in missing:
        got = generate_title_for(idx, c)
        click.echo(f"  {c.n:>3}  {got or '(could not generate)'}")
    titles.warn_once_if_unavailable()


def generate_title_for(idx: Index, conv: ConversationRow) -> str | None:
    root = conv.instances[-1]
    turns = idx.turns(root.id)
    user_msgs = [t.content for t in turns if t.author == "user"]
    assistant_first = next((t.content for t in turns if t.author == "agent"), "")
    got = titles.generate_title(root.session_id, user_msgs, assistant_first)
    if got:
        idx.set_generated_title(conv.id, got)
    return got


@main.command("exclude")
@click.argument("handle")
@click.option("--undo", "restore", is_flag=True, help="include it again")
def exclude_cmd(handle: str, restore: bool) -> None:
    """Exclude a conversation from the index (its text is dropped; the file is untouched)."""
    idx = _open_index(quiet=True)
    conv = _resolve(idx, handle)
    idx.set_excluded(conv.id, not restore)
    click.echo(("excluded from index: " if not restore else "included again: ") + conv.title)


@main.command("mcp")
@click.option("--http", "port", type=int, default=None, help="serve over HTTP on this port instead of stdio")
def mcp_cmd(port: int | None) -> None:
    """Start the MCP server over the archive (stdio by default)."""
    from ferry.mcp_server import serve

    serve(port=port)


@main.command("mcp-config")
def mcp_config_cmd() -> None:
    """Print config blocks for Claude Code, Codex, and Claude Desktop."""
    from ferry.mcp_server import config_blocks

    click.echo(config_blocks())


@main.command("doctor")
def doctor_cmd() -> None:
    """Stores, agents, tools, and every file the adapters could not parse."""
    import shutil

    idx = Index()
    report = idx.rebuild()
    click.echo(f"ferry {__version__} · cache {display_path(CACHE_DIR)} · index {display_path(INDEX_DB)}")
    click.echo("")
    for a in adapters.agents():
        home = adapters.home_for(a)
        state = click.style("✗ " + report.store_errors[a], fg="red") if a in report.store_errors else click.style("✓", fg="green")
        inj = "read+write" if adapters.can_inject(a) else "read-only"
        click.echo(f"  {agent_icon(a)} {agent_label(a):<14} {display_path(home):<60} {inj:<10} {state}")
    click.echo("")
    for tool in ("claude", "codex", "opencode", "open", "handoff"):
        click.echo(f"  {tool:<10} {shutil.which(tool) or click.style('not on PATH', dim=True)}")
    st = idx.stats()
    click.echo("")
    click.echo(f"  {st['conversations']} conversations · instances {st['instances']} · {st['turns']} turns · last build {st['last_build']}")
    issues = [i for i in report.issues]
    if issues:
        click.echo("")
        click.echo(click.style(f"  {len(issues)} file(s) not parsed — each names the file and the field; the fix lives in one adapter file:", fg="yellow"))
        for i in issues[:50]:
            click.echo(f"    {i}")
    reason = titles.claude_unavailable_reason()
    if reason:
        click.echo(click.style(f"  titles: {reason}", fg="yellow"))


def _resolve(idx: Index, handle: str) -> ConversationRow:
    try:
        return idx.resolve(handle)
    except LookupError as exc:
        raise click.ClickException(str(exc))


def _choose_instance(conv: ConversationRow, which: str | None):
    if which:
        for i in conv.instances:
            if i.agent == which:
                return i
        raise click.ClickException(f"no {which} instance in this conversation (has: {', '.join(conv.agents)})")
    return conv.newest


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
