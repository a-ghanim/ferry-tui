"""The one screen.

Layout rules (from the spec, treated as rules): no box borders, transparent
background, hierarchy through weight and dimming, two accent colours (one per
agent family), generous vertical padding, readable prose in the preview, a
one-line dim footer with the four keys that matter.

Everything the screen shows comes from the index; nothing here reads a raw
session file.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from ferry import __version__, adapters, launch, titles, transfer
from ferry.adapters import agent_icon, agent_label
from ferry.index import ConversationRow, Index, InstanceRow
from ferry.paths import display_path

INK = "#edf3ee"
MUTED = "#91a6a6"
CLAUDE = "#ffae82"
CODEX = "#7cdecf"
ACCENT = {"codex": CODEX, "claude": CLAUDE, "claude-cowork": CLAUDE, "opencode": CODEX}


def accent(agent: str) -> str:
    return ACCENT.get(agent, "white")


def local(ts: str, fmt: str = "%Y-%m-%d") -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except ValueError:
        return ts[:10]


def fit(s: str, n: int) -> str:
    s = s or ""
    if n <= 1:
        return s[:n]
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def turns_label(count: int) -> str:
    return f"{count} {'turn' if count == 1 else 'turns'}"


def who_prefix(author: str, agent: str) -> str:
    if author == "user":
        return "you ›"
    return {"claude": "claude ›", "claude-cowork": "claude ›", "codex": "codex ›", "opencode": "opencode ›"}.get(agent, f"{agent} ›")


class ResponsiveBody(Horizontal):
    """Own the width breakpoint because resize events are delivered to widgets."""

    def on_resize(self, event: events.Resize) -> None:
        app = self.app
        narrow = event.size.width < 100
        if app.screen.has_class("narrow") != narrow:
            app.screen.set_class(narrow, "narrow")
            if getattr(app, "_columns", None) is not None:
                app.call_after_refresh(app.reload_rows, True)


# --------------------------------------------------------------------------- #
# Modal dialogs — one quiet panel, keys in the last line
# --------------------------------------------------------------------------- #
class Dialog(ModalScreen):
    DEFAULT_CSS = """
    Dialog { align: center middle; background: #000000 55%; }
    Dialog > Vertical { width: 88; max-width: 94%; height: auto; max-height: 90%; overflow-y: auto; background: #17272e; padding: 2 3; border-left: tall #ffae82; }
    Dialog .title { text-style: bold; color: #edf3ee; margin-bottom: 1; }
    Dialog .dim { color: #91a6a6; }
    Dialog Input { border: none; background: #243a40; color: #edf3ee; padding: 0 1; margin-top: 1; }
    Dialog Input:focus { border: none; background: #315057; }
    Dialog #tab-panel:focus { border: none; }
    Dialog .keys { color: #7cdecf; margin-top: 1; }
    Dialog #tab-agent, Dialog #tab-mode { margin: 1 0; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_enter_key(self) -> None:
        """Called by the App when Enter is pressed while this dialog is up."""


class TextDialog(Dialog):
    def __init__(self, title: str, hint: str, value: str, keys: str = "enter save · esc cancel") -> None:
        super().__init__()
        self._title, self._hint, self._value, self._keys = title, hint, value, keys

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="title", markup=False)
            yield Static(self._hint, classes="dim", markup=False)
            yield Input(value=self._value, id="value")
            yield Static(self._keys, classes="keys")

    def on_mount(self) -> None:
        inp = self.query_one("#value", Input)
        inp.focus()
        inp.cursor_position = len(self._value)

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_enter_key(self) -> None:
        self.dismiss(self.query_one("#value", Input).value)


class ConfirmDialog(Dialog):
    BINDINGS = [Binding("escape", "cancel", show=False), Binding("enter", "ok", show=False), Binding("y", "ok", show=False), Binding("n", "cancel", show=False)]

    def __init__(self, title: str, body: str, keys: str = "enter confirm · esc cancel") -> None:
        super().__init__()
        self._title, self._body, self._keys = title, body, keys

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="title", markup=False)
            yield Static(self._body, classes="dim", markup=False)
            yield Static(self._keys, classes="keys")

    def action_ok(self) -> None:
        self.dismiss(True)

    def on_enter_key(self) -> None:
        self.dismiss(True)


class TabDialog(Dialog):
    """Target agent (if several), transfer mode, folders. Enter moves."""

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("enter", "go", show=False, priority=True),
        Binding("m", "cycle_mode", show=False),
        Binding("a", "cycle_agent", show=False),
        Binding("f", "edit_folders", show=False),
    ]

    def __init__(self, conv: ConversationRow, inst: InstanceRow, targets: list[str], count: int = 1) -> None:
        super().__init__()
        self.conv, self.inst, self.targets, self.count = conv, inst, targets, count
        self.agent_i = 0
        default = transfer.choose_mode(inst.tokens_raw)
        self.mode_i = transfer.MODES.index(conv.prefs.get("mode") or default) if (conv.prefs.get("mode") or default) in transfer.MODES else 0
        folders = [f for f in (conv.prefs.get("folders") or inst.roots or ([inst.cwd] if inst.cwd else [])) if f and Path(f).is_dir()]
        home = str(Path.home())
        self.folders_default = ", ".join(("~" + f[len(home):]) if f.startswith(home + "/") else f for f in folders)

    @property
    def target(self) -> str:
        return self.targets[self.agent_i]

    @property
    def mode(self) -> str:
        return transfer.MODES[self.mode_i]

    def compose(self) -> ComposeResult:
        with Vertical(id="tab-panel"):
            yield Static("", id="tab-title", classes="title", markup=False)
            yield Static("", id="tab-summary", classes="dim")
            yield Static("", id="tab-agent")
            yield Static("", id="tab-mode")
            yield Static("Folders the receiving agent may use (comma-separated; empty = none):", classes="dim")
            yield Input(value=self.folders_default, id="folders")
            yield Static("", id="tab-hint", classes="dim")
            yield Static("enter move · a target · m mode · f edit folders · esc cancel", classes="keys")

    def on_mount(self) -> None:
        self._refresh()
        # Letters must reach the shortcuts, so the panel (not the text field) starts focused.
        panel = self.query_one("#tab-panel", Vertical)
        panel.can_focus = True
        panel.focus()

    def action_edit_folders(self) -> None:
        inp = self.query_one("#folders", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def action_cancel(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#tab-panel", Vertical).focus()  # leave the field, keep the dialog
            return
        self.dismiss(None)

    def _refresh(self) -> None:
        n = f" ({self.count} selected)" if self.count > 1 else ""
        self.query_one("#tab-title", Static).update(f"Carry this conversation across{n}\n{self.conv.title}")
        self.query_one("#tab-summary", Static).update(f"{agent_label(self.inst.agent)} · {transfer.summary_line(self.inst)}")
        agents_txt = Text("Target:  ")
        for i, a in enumerate(self.targets):
            style = "bold reverse" if i == self.agent_i else "dim"
            agents_txt.append(f" {agent_icon(a)} {agent_label(a)} ", style=style)
            agents_txt.append("  ")
        self.query_one("#tab-agent", Static).update(agents_txt)
        modes = Text("Mode:    ")
        desc = {"raw": "everything", "compact": "no tool output", "brief": "summary"}
        default = transfer.choose_mode(self.inst.tokens_raw)
        for i, m in enumerate(transfer.MODES):
            style = "bold reverse" if i == self.mode_i else "dim"
            modes.append(f" {m} · {desc[m]}{' · default' if m == default else ''} ", style=style)
            modes.append("  ")
        self.query_one("#tab-mode", Static).update(modes)
        hint = "Folders inherit, permissions don't: the receiving agent will prompt as usual. A new file is written; originals are never touched; `ferry undo` reverses it."
        if self.inst.agent == "claude" and self.target == "codex":
            hint += "\nCodex can also import this natively: open the Codex app › /import."
        if self.inst.tokens_raw >= transfer.COMPACT_THRESHOLD_TOKENS and self.mode == "raw":
            hint += f"\n~{self.inst.tokens_raw // 1000}k tokens may overflow the receiving agent; compact is recommended."
        self.query_one("#tab-hint", Static).update(hint)

    @on(Input.Submitted, "#folders")
    def _folders_submitted(self) -> None:
        self.action_go()

    def on_enter_key(self) -> None:
        self.action_go()

    def action_cycle_mode(self) -> None:
        self.mode_i = (self.mode_i + 1) % len(transfer.MODES)
        self._refresh()

    def action_cycle_agent(self) -> None:
        self.agent_i = (self.agent_i + 1) % len(self.targets)
        self._refresh()

    def action_go(self) -> None:
        raw = self.query_one("#folders", Input).value
        folders = [str(Path(f.strip()).expanduser()) for f in raw.split(",") if f.strip()]
        self.dismiss({"to": self.target, "mode": self.mode, "folders": folders})


# --------------------------------------------------------------------------- #
# The app
# --------------------------------------------------------------------------- #
class FerryApp(App):
    TITLE = "ferry"
    CSS = """
    Screen { background: transparent; color: #edf3ee; }
    #masthead { height: 6; padding: 1 2 0 2; background: #14252c; }
    #boat { width: 13; height: 4; color: #ffae82; }
    #brand { width: 1fr; height: 4; }
    #route { width: auto; max-width: 42%; height: 4; color: #91a6a6; text-align: right; }
    #search-wrap { height: 3; padding: 0 2 1 2; background: #14252c; }
    #search-glyph { width: 3; height: 2; color: #7cdecf; background: #243a40; padding-left: 1; }
    #search { width: 1fr; height: 2; border: none; background: #243a40; color: #edf3ee; padding: 0 1; }
    #search:focus { border: none; background: #315057; }
    #search > .input--placeholder { color: #91a6a6; }
    #body { height: 1fr; }
    #left { width: 52%; height: 1fr; background: transparent; padding-left: 1; }
    .section-label { height: 2; padding: 0 1; color: #7cdecf; text-style: bold; }
    #list { height: 1fr; background: transparent; scrollbar-size: 1 1; overflow-x: hidden; }
    #list > .datatable--cursor { background: #29494d; color: #edf3ee; text-style: bold; }
    #list > .datatable--hover { background: #1b3035; }
    #empty { padding: 2 2; color: #91a6a6; display: none; }
    #rule { width: 1; height: 1fr; color: #315057; }
    #right { width: 1fr; height: 1fr; }
    #preview-label { padding-left: 2; }
    #preview { width: 1fr; height: 1fr; padding: 0 3 1 3; background: transparent; scrollbar-size: 1 1; }
    #preview-body { width: 1fr; }
    #footer { height: 2; color: #91a6a6; padding: 0 2; background: #14252c; }
    Screen.narrow #left { width: 1fr; }
    Screen.narrow #right, Screen.narrow #rule { display: none; }
    Screen.narrow.previewing #left { display: none; }
    Screen.narrow.previewing #right { display: block; }
    Screen.narrow #route { max-width: 35%; }
    Screen.narrow #masthead { height: 5; padding: 0 1; }
    Screen.narrow #search-wrap { padding: 0 1 1 1; }
    """
    BINDINGS = [
        Binding("enter", "open", "open", show=False, priority=True),
        Binding("tab", "move", "move", show=False, priority=True),
        Binding("slash", "focus_search", "search", show=False),
        Binding("q", "quit", "quit", show=False),
        Binding("escape", "escape", show=False),
        Binding("space", "mark", show=False),
        Binding("r", "rename", show=False),
        Binding("d", "archive", show=False),
        Binding("t", "tags", show=False),
        Binding("g", "group", show=False),
        Binding("e", "expand", show=False),
        Binding("2", "second_opinion", show=False),
        Binding("u", "undo", show=False),
        Binding("question_mark", "help", show=False),
        Binding("p", "preview", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(ansi_color=False)
        self.index = Index()
        self.convs: list[ConversationRow] = []
        self.rows: list[tuple[str, ConversationRow, InstanceRow | None]] = []  # (kind, conv, inst)
        self.marked: set[str] = set()
        self.expanded: set[str] = set()
        self.grouped = False
        self.query = ""
        self._title_inflight: set[str] = set()
        self._columns = None
        self._search_timer = None
        self._first_report = None
        self._previewing = False

    # ---- layout ---------------------------------------------------------- #
    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static("    ╭─╮\n ╭──┤•├──╮\n ╰───────╯ ≋", id="boat", markup=False)
            yield Static(Text.assemble(("ferry", f"bold {INK}"), (f"  v{__version__}\n", MUTED), ("Your conversations, all in one tide.\n", MUTED), ("Pick up where you left off.", f"bold {CLAUDE}")), id="brand")
            yield Static("", id="route")
        with Horizontal(id="search-wrap"):
            yield Static("⌕ ", id="search-glyph")
            yield Input(placeholder="Search titles, messages, or tags…   /", id="search")
        with ResponsiveBody(id="body"):
            with Vertical(id="left"):
                yield Static("CONVERSATIONS", id="list-label", classes="section-label")
                yield DataTable(id="list", show_header=False, cursor_type="row", zebra_stripes=False, show_cursor=True, cursor_foreground_priority="renderable", cursor_background_priority="css")
                yield Static("", id="empty")
            yield Static("", id="rule")
            with Vertical(id="right"):
                yield Static("TRANSCRIPT  ·  p to focus", id="preview-label", classes="section-label")
                with VerticalScroll(id="preview"):
                    yield Static("", id="preview-body")
        keys = Text()
        for key, label in ((" ↵ ", "open"), (" ⇥ ", "carry"), (" / ", "search"), (" ? ", "keys"), (" q ", "quit")):
            keys.append(key, style=f"bold #14252c on {CODEX if key.strip() == '⇥' else MUTED}")
            keys.append(f" {label}   ", style=MUTED)
        yield Static(keys, id="footer")

    def on_mount(self) -> None:
        table = self.query_one("#list", DataTable)
        self._columns = []  # built per width in reload_rows
        self.query_one("#rule", Static).update("\n".join("│" for _ in range(200)))
        self._first_report = self.index.rebuild()
        self._responsive_layout()
        self.reload_rows()
        table.focus()
        self.call_after_refresh(self.reload_rows, True)
        self.set_interval(4.0, self._poll)
        for agent, err in self._first_report.store_errors.items():
            self.notify(err, title=agent_label(agent), severity="warning", timeout=8)
        if self._first_report.issues:
            self.notify(f"{len(self._first_report.issues)} file(s) could not be parsed — run `ferry doctor`", severity="warning", timeout=8)
        reason = titles.claude_unavailable_reason()
        if reason:
            self.notify(reason, severity="warning", timeout=8)

    def _responsive_layout(self) -> None:
        self.screen.set_class(self.size.width < 100, "narrow")
        self.screen.set_class(self._previewing, "previewing")

    def action_preview(self) -> None:
        self._previewing = not self._previewing
        self._responsive_layout()
        self.query_one("#preview" if self._previewing else "#list").focus()
        self.query_one("#preview-label", Static).update(
            "TRANSCRIPT  ·  p back · ↑↓ scroll" if self._previewing else "TRANSCRIPT  ·  p to focus"
        )
        self.call_after_refresh(self.reload_rows, True)

    # ---- rows ------------------------------------------------------------- #
    def _widths(self) -> dict:
        """Keep dates aligned while giving titles and their metadata room."""
        left = self.query_one("#left").size.width
        if left <= 0:
            left = int(self.size.width * 0.52) if self.size.width else 90
        total = max(30, left - 2)  # scrollbar
        pad = 2  # DataTable pads each cell by one space per side
        date_w = 10 if total >= 56 else 5
        title_w = max(12, total - (1 + 2 + date_w) - pad * 4)
        return {"title": title_w, "date": date_w}

    def _rebuild_columns(self, table: DataTable, plan: dict) -> None:
        table.clear(columns=True)
        cols = [("mark", 1), ("icon", 2), ("title", plan["title"]), ("date", plan["date"])]
        self._columns = [table.add_column(name, width=w, key=name) for name, w in cols]
        self._plan = plan

    def reload_rows(self, keep_cursor: bool = True) -> None:
        table = self.query_one("#list", DataTable)
        if self._columns is None:
            return
        current_key = None
        if keep_cursor and self.rows and 0 <= table.cursor_row < len(self.rows):
            kind, conv, inst = self.rows[table.cursor_row]
            current_key = (kind, conv.id, inst.id if inst else None)
        self.convs = self.index.conversations(query=self.query or None)
        self._update_route()
        plan = self._widths()
        self._rebuild_columns(table, plan)
        self.rows = []
        title_w, date_w = plan["title"], plan["date"]
        date_fmt = "%Y-%m-%d" if date_w >= 10 else "%m-%d"
        ordered: list[tuple[str | None, ConversationRow]] = []
        if self.grouped:
            by_tag: dict[str, list[ConversationRow]] = {}
            for c in self.convs:
                key = c.tags[0] if c.tags else "untagged"
                by_tag.setdefault(key, []).append(c)
            for tag in sorted(by_tag, key=lambda t: (t == "untagged", t)):
                ordered.append((tag, by_tag[tag][0]))
                ordered.extend((None, c) for c in by_tag[tag])
        else:
            ordered = [(None, c) for c in self.convs]
        for header, conv in ordered:
            if header is not None:
                table.add_row("", "", Text(f"  # {header.upper()}", style=f"bold {CODEX}"), "", key=f"hdr:{header}")
                self.rows.append(("hdr", conv, None))
                continue
            mark = Text("◆" if conv.id in self.marked else "∙", style=f"bold {CLAUDE if conv.id in self.marked else MUTED}")
            icon = Text("")
            for a in conv.agents:
                icon.append(agent_icon(a), style=f"bold {accent(a)}")
            title = Text(fit(conv.title, title_w), style=f"bold {INK}" if conv.has_title else MUTED)
            meta = f"{agent_label(conv.newest.agent)} · {turns_label(conv.newest.turns)}"
            if conv.newest.from_agent:
                meta += f" · from {agent_label(conv.newest.from_agent)}"
            if conv.tags:
                meta += "  " + " ".join("#" + tag for tag in conv.tags[:2])
            elif conv.folder:
                meta += "  " + display_path(conv.folder)
            title.append("\n" + fit(meta, title_w), style=MUTED)
            date = Text(local(conv.updated_at, date_fmt), style=MUTED)
            table.add_row(mark, icon, title, date, height=3, key=f"conv:{conv.id}")
            self.rows.append(("conv", conv, None))
            if conv.id in self.expanded:
                for inst in conv.instances:
                    via = f" ← {agent_label(inst.from_agent)}" if inst.from_agent else ""
                    line = Text(fit(f"↳ {agent_label(inst.agent)} · {turns_label(inst.turns)}{via}", title_w), style=f"bold {accent(inst.agent)}")
                    line.append("\n" + fit(display_path(inst.cwd) or "no folder", title_w), style=MUTED)
                    icell = Text(agent_icon(inst.agent), style=f"bold {accent(inst.agent)}")
                    dcell = Text(local(inst.updated_at, date_fmt), style=MUTED)
                    table.add_row("", icell, line, dcell, height=2, key=f"inst:{inst.id}")
                    self.rows.append(("inst", conv, inst))
        empty = self.query_one("#empty", Static)
        table.display = bool(self.rows)
        label = "SEARCH RESULTS" if self.query else "BY TAG" if self.grouped else "CONVERSATIONS"
        self.query_one("#list-label", Static).update(f"◈  {label}   {len(self.convs)}")
        if not self.rows:
            empty.update(self._empty_text())
            empty.styles.display = "block"
            self.query_one("#preview-body", Static).update(Text("The next conversation is just a search away.\n\nSelect a conversation to read it here.", style="dim"))
        else:
            empty.styles.display = "none"
            idx = 0
            if current_key:
                for i, (kind, conv, inst) in enumerate(self.rows):
                    if (kind, conv.id, inst.id if inst else None) == current_key:
                        idx = i
                        break
            table.move_cursor(row=idx)
            self._show_preview(idx)

    def _update_route(self) -> None:
        route = Text()
        present = sorted({agent for conv in self.convs for agent in conv.agents})
        for position, agent in enumerate(present):
            if position:
                route.append("  ⇄  ", style=MUTED)
            route.append(agent_icon(agent), style=f"bold {accent(agent)}")
            if self.size.width >= 120:
                route.append(f" {agent_label(agent)}", style=INK)
        if present:
            route.append("\n", style=MUTED)
        noun = "conversation" if len(self.convs) == 1 else "conversations"
        route.append(f"{len(self.convs)} {noun}", style=MUTED)
        if self.marked:
            route.append(f"  ·  {len(self.marked)} selected", style=f"bold {CLAUDE}")
        elif self.query:
            route.append(f"\nmatching “{fit(self.query, 18)}”", style=MUTED)
        else:
            route.append("\nall on this machine", style=MUTED)
        self.query_one("#route", Static).update(route)

    def _empty_text(self) -> Text:
        if self.query:
            return Text(f"No conversations on this shore.\n\nNothing matches “{self.query}”.\nTry another word, or / then esc to clear.", style="dim")
        issues = [i for i in self.index.issues() if i.field == "store"]
        if issues:
            return Text("\n".join(i.message for i in issues), style="dim")
        stores = "\n".join(f"  {agent_label(a)}  {display_path(adapters.home_for(a))}" for a in adapters.agents())
        return Text(f"Ready when you are.\n\nStart a conversation in an agent, then come back.\nFerry checks these stores automatically:\n\n{stores}", style="dim")

    def _current(self) -> tuple[str, ConversationRow, InstanceRow | None] | None:
        table = self.query_one("#list", DataTable)
        if not self.rows or not (0 <= table.cursor_row < len(self.rows)):
            return None
        return self.rows[table.cursor_row]

    # ---- preview ------------------------------------------------------------ #
    @on(DataTable.RowHighlighted)
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_preview(event.cursor_row)

    def _show_preview(self, row: int) -> None:
        if not (0 <= row < len(self.rows)):
            return
        kind, conv, inst = self.rows[row]
        body = self.query_one("#preview-body", Static)
        if kind == "hdr":
            body.update(Text(f"# {conv.tags[0] if conv.tags else 'untagged'}", style=f"bold {CODEX}"))
            return
        text = Text()
        text.append("SELECTED CONVERSATION\n", style=f"bold {CODEX}")
        text.append(conv.title, style=f"bold {INK}")
        text.append("\n\n")
        newest = inst or conv.newest
        text.append(f" {agent_icon(newest.agent)} {agent_label(newest.agent).upper()} ", style=f"bold #14252c on {accent(newest.agent)}")
        meta = f"  {local(newest.updated_at, '%b %d · %H:%M')}\n{display_path(newest.cwd) or 'no folder'}"
        if conv.tags:
            meta += "  ·  " + " ".join("#" + t for t in conv.tags)
        text.append(meta + "\n", style=MUTED)
        text.append(transfer.summary_line(newest), style=MUTED)
        text.append("\n", style=MUTED)
        if newest.from_agent:
            parent = self.index.instance(newest.parent_instance_id) if newest.parent_instance_id else None
            carried = f" · {parent.turns} source turns" if parent else ""
            text.append(f"↳  carried from {agent_label(newest.from_agent)}{carried}\n", style=f"bold {accent(newest.from_agent)}")
        if not newest.openable:
            text.append((newest.extra.get("note") or "This instance cannot be reopened from outside its app.") + "\n", style=MUTED)
        if len(conv.instances) > 1 and not inst:
            text.append(f"{len(conv.instances)} linked instances · e expands\n", style=MUTED)
        text.append("\n" + "─" * 22 + "\n\n", style="#315057")
        blocks = [text]
        pairs = [(inst, self.index.turns(inst.id))] if inst else self.index.conversation_turns(conv)
        shown = 0
        for i, turns in pairs:
            if len(pairs) > 1:
                hdr = f"● {agent_label(i.agent)} · {turns_label(i.turns)}"
                if i.from_agent:
                    hdr += f" · moved from {agent_label(i.from_agent)}"
                blocks.append(Text(hdr + "\n\n", style=f"bold {accent(i.agent)}"))
            for t in turns:
                role_style = MUTED if t.author == "user" else f"bold {accent(i.agent)}"
                blocks.append(Text("▌  " + who_prefix(t.author, i.agent), style=role_style))
                body_text = t.content.strip()
                if body_text.startswith("[handoff]") and any(p.id == i.parent_instance_id for p, _ in pairs):
                    blocks.append(Padding(Text(body_text.splitlines()[0], style=MUTED), (0, 0, 0, 3)))
                    blocks.append(Text(""))
                    continue
                message = Markdown(body_text, code_theme="ansi_dark", hyperlinks=False) if t.author != "user" else Text(body_text)
                blocks.append(Padding(message, (0, 0, 0, 3)))
                blocks.append(Text(""))
                shown += 1
                if shown >= 300:
                    blocks.append(Text("… preview truncated; `ferry show` prints everything\n", style=MUTED))
                    break
            if shown >= 300:
                break
        if shown == 0:
            blocks.append(Text("No user or assistant turns were found in this session.", style=MUTED))
        body.update(Group(*blocks))
        self.query_one("#preview", VerticalScroll).scroll_home(animate=False)
        self._maybe_title(conv)

    # ---- titles (lazy) ---------------------------------------------------- #
    def _maybe_title(self, conv: ConversationRow) -> None:
        if titles.claude_unavailable_reason():
            return
        table = self.query_one("#list", DataTable)
        lo = max(0, table.cursor_row - 3)
        hi = min(len(self.rows), table.cursor_row + max(12, table.size.height))
        candidates = [conv] + [c for kind, c, _ in self.rows[lo:hi] if kind == "conv"]
        for c in candidates:
            if c.has_title or c.id in self._title_inflight:
                continue
            self._title_inflight.add(c.id)
            self._gen_title(c)

    @work(thread=True, group="titles", exit_on_error=False)
    def _gen_title(self, conv: ConversationRow) -> None:
        from ferry.cli import generate_title_for

        got = generate_title_for(self.index, conv)
        self.call_from_thread(self._title_done, conv, got)

    def _title_done(self, conv: ConversationRow, got: str | None) -> None:
        self._title_inflight.discard(conv.id)
        if got:
            self.reload_rows(keep_cursor=True)
        else:
            reason = titles.claude_unavailable_reason()
            if reason:
                self.notify(reason, severity="warning", timeout=6)

    # ---- live refresh ------------------------------------------------------- #
    @work(thread=True, group="poll", exclusive=True, exit_on_error=False)
    def _poll(self) -> None:
        report = self.index.rebuild()
        if report.changed:
            self.call_from_thread(self.reload_rows, True)

    # ---- search ------------------------------------------------------------- #
    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self.query = event.value
        if self._search_timer:
            self._search_timer.stop()
        self._search_timer = self.set_timer(0.15, lambda: self.reload_rows(keep_cursor=False))

    @on(Input.Submitted, "#search")
    def _search_submit(self) -> None:
        self.query_one("#list", DataTable).focus()

    def action_focus_search(self) -> None:
        if self._previewing:
            self.action_preview()
        self.query_one("#search", Input).focus()

    def action_escape(self) -> None:
        search = self.query_one("#search", Input)
        if self.focused is search:
            if search.value:
                search.value = ""
            else:
                self.query_one("#list", DataTable).focus()
        elif self.marked:
            self.marked.clear()
            self.reload_rows()
        elif self._previewing:
            self.action_preview()

    # ---- selection ---------------------------------------------------------- #
    def _targets(self) -> list[ConversationRow]:
        if self.marked:
            return [c for c in self.convs if c.id in self.marked]
        cur = self._current()
        return [cur[1]] if cur and cur[0] != "hdr" else []

    def action_mark(self) -> None:
        cur = self._current()
        if not cur or cur[0] == "hdr":
            return
        conv = cur[1]
        if conv.id in self.marked:
            self.marked.discard(conv.id)
        else:
            self.marked.add(conv.id)
        self._update_route()
        table = self.query_one("#list", DataTable)
        row = table.cursor_row
        self.reload_rows(keep_cursor=True)
        if row + 1 < len(self.rows):
            table.move_cursor(row=row + 1)

    def action_expand(self) -> None:
        cur = self._current()
        if not cur:
            return
        conv = cur[1]
        if conv.id in self.expanded:
            self.expanded.discard(conv.id)
        else:
            self.expanded.add(conv.id)
        self.reload_rows(keep_cursor=True)

    def action_group(self) -> None:
        self.grouped = not self.grouped
        self.reload_rows(keep_cursor=True)

    # ---- Enter ------------------------------------------------------------- #
    def action_open(self) -> None:
        if isinstance(self.screen, Dialog):
            self.screen.on_enter_key()
            return
        if self.focused is self.query_one("#search", Input):
            self.query_one("#list", DataTable).focus()
            return
        cur = self._current()
        if not cur or cur[0] == "hdr":
            return
        items: list[tuple[ConversationRow, InstanceRow]] = []
        if cur[0] == "inst" and cur[2] is not None:
            items = [(cur[1], cur[2])]
        else:
            items = [(c, c.newest) for c in self._targets()]
        self._open_many(items)

    def _open_many(self, items: list[tuple[ConversationRow, InstanceRow]]) -> None:
        plans = []
        for conv, inst in items:
            p = launch.plan(inst.agent, inst.session_id, inst.cwd, inst.openable, generated=inst.is_moved)
            if p.kind == "none":
                self.notify(p.description, title=conv.title, severity="warning", timeout=8)
                continue
            plans.append((conv, inst, p))
        gui = [x for x in plans if x[2].kind == "open"]
        execs = [x for x in plans if x[2].kind == "exec"]
        for conv, inst, p in gui:
            launch.run(p)
            self.notify(f"opened in {agent_label(inst.agent)}", title=conv.title, timeout=4)
        for conv, inst, p in execs:
            roots = ", ".join(display_path(r) for r in (inst.roots or [inst.cwd])) or "no folder"
            if os.environ.get("FERRY_NO_LAUNCH"):
                self.notify(f"ready in {agent_label(inst.agent)} · {roots}", title=conv.title, timeout=6)
                continue
            try:
                with self.suspend():
                    print(f"\n→ {conv.title}\n  {agent_label(inst.agent)} · {roots}\n")
                    launch.run(p)
            except SuspendNotSupported:
                self.notify(f"open it from a terminal: cd {p.cwd!r} && {Path(p.argv[0]).name} {' '.join(p.argv[1:])}", title=conv.title, timeout=10)
        if execs:
            self.reload_rows()

    # ---- Tab --------------------------------------------------------------- #
    def action_move(self) -> None:
        if isinstance(self.screen, Dialog):
            return
        if self.focused is self.query_one("#search", Input):
            self.query_one("#list", DataTable).focus()
            return
        convs = self._targets()
        if not convs:
            return
        conv = convs[0]
        inst = conv.newest
        cur = self._current()
        if cur and cur[0] == "inst" and cur[2] is not None and not self.marked:
            inst = cur[2]
        targets = transfer.targets_for(inst)
        if not targets:
            self.notify("no other agent can receive this conversation", severity="warning")
            return
        if transfer.is_live(inst):
            self.notify("still active — finish the turn first", severity="warning")
            return

        def done(choice) -> None:
            if not choice:
                return
            self._do_moves(convs, inst if len(convs) == 1 else None, choice)

        self.push_screen(TabDialog(conv, inst, targets, count=len(convs)), done)

    @work(thread=True, group="move", exclusive=True, exit_on_error=False)
    def _do_moves(self, convs: list[ConversationRow], inst: InstanceRow | None, choice: dict) -> None:
        results = []
        for conv in convs:
            src = inst or conv.newest
            folders = choice["folders"] if len(convs) == 1 else (conv.prefs.get("folders") or src.roots or ([src.cwd] if src.cwd else []))
            try:
                res = transfer.move(self.index, conv, src, choice["to"], mode=choice["mode"], folders=folders or None)
                results.append((conv, res))
                self.call_from_thread(self.notify, f"{res.banner} → {agent_label(res.to_agent)}", title=conv.title, timeout=6)
            except transfer.TransferError as exc:
                self.call_from_thread(self.notify, str(exc), title=conv.title, severity="error", timeout=10)
        self.call_from_thread(self._after_moves, results)

    def _after_moves(self, results) -> None:
        self.marked.clear()
        self.reload_rows()
        if not results:
            return
        items = []
        for conv, res in results:
            new_conv = self.index.conversation(res.conversation_id) or conv
            new_inst = self.index.instance(res.new_instance_id)
            if new_inst:
                items.append((new_conv, new_inst))
        self._open_many(items)

    # ---- second opinion ----------------------------------------------------- #
    def action_second_opinion(self) -> None:
        cur = self._current()
        if not cur or cur[0] == "hdr":
            return
        conv = cur[1]
        inst = cur[2] or conv.newest
        targets = transfer.targets_for(inst)
        if not targets:
            self.notify("no other agent can receive this conversation", severity="warning")
            return
        body = f"Fork to {', '.join(agent_label(t) for t in targets)} and open the native session alongside.\n{transfer.summary_line(inst)} · mode {transfer.choose_mode(inst.tokens_raw)}"

        def done(ok) -> None:
            if ok:
                self._do_fork(conv, inst)

        self.push_screen(ConfirmDialog(f"Second opinion: {conv.title}", body), done)

    @work(thread=True, group="move", exclusive=True, exit_on_error=False)
    def _do_fork(self, conv: ConversationRow, inst: InstanceRow) -> None:
        try:
            results = transfer.second_opinion(self.index, conv, inst)
        except transfer.TransferError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=10)
            return
        items = [(conv, inst)]
        for res in results:
            new_inst = self.index.instance(res.new_instance_id)
            if new_inst:
                items.append((self.index.conversation(res.conversation_id) or conv, new_inst))
        self.call_from_thread(self._after_fork, items)

    def _after_fork(self, items) -> None:
        self.reload_rows()
        # GUI agents open beside the terminal; terminal agents run one after another.
        self._open_many(items)

    # ---- rename / archive / tags / undo ------------------------------------ #
    def action_rename(self) -> None:
        cur = self._current()
        if not cur or cur[0] == "hdr":
            return
        conv = cur[1]

        def done(value) -> None:
            if not value or value.strip() == conv.title:
                return
            try:
                where = transfer.rename(self.index, conv, value)
            except transfer.TransferError as exc:
                self.notify(str(exc), severity="error")
                return
            self.notify(f"renamed in {where}", timeout=4)
            self.reload_rows()

        self.push_screen(TextDialog("Rename", "Written back to the agent's own store where it keeps a title.", conv.title), done)

    def action_archive(self) -> None:
        convs = self._targets()
        if not convs:
            return
        names = "\n".join(f"  {c.title}" for c in convs[:8]) + ("\n  …" if len(convs) > 8 else "")
        body = f"Move the session file(s) to ~/.cache/ferry/archive/. Nothing is deleted; u undoes the last one.\n{names}"

        def done(ok) -> None:
            if not ok:
                return
            for conv in convs:
                for inst in conv.instances:
                    try:
                        transfer.archive(self.index, conv, inst)
                    except transfer.TransferError as exc:
                        self.notify(str(exc), title=conv.title, severity="error")
            self.marked.clear()
            self.notify(f"archived {len(convs)}", timeout=4)
            self.reload_rows(keep_cursor=False)

        self.push_screen(ConfirmDialog("Archive", body), done)

    def action_tags(self) -> None:
        cur = self._current()
        if not cur or cur[0] == "hdr":
            return
        conv = cur[1]
        existing = " ".join(t for t, _ in self.index.all_tags()[:12])

        def done(value) -> None:
            if value is None:
                return
            if value.strip() == "?":
                self._infer_tags(conv)
                return
            tags_ = self.index.set_tags(conv.id, value.replace("#", " ").split())
            self.notify(" ".join("#" + t for t in tags_) or "no tags", title=conv.title, timeout=4)
            self.reload_rows()

        self.push_screen(TextDialog("Tags", f"Space-separated. Type ? to ask claude -p. In use: {existing}", " ".join(conv.tags)), done)

    @work(thread=True, group="titles", exit_on_error=False)
    def _infer_tags(self, conv: ConversationRow) -> None:
        from ferry import tags as tagmod

        got = tagmod.generate_tags(conv.id, self.index._user_text_sample(conv.id), [t for t, _ in self.index.all_tags()])
        if got:
            self.index.rebuild()
            self.call_from_thread(self.notify, " ".join("#" + t for t in got), title=conv.title, timeout=4)
            self.call_from_thread(self.reload_rows, True)
        else:
            self.call_from_thread(self.notify, titles.claude_unavailable_reason() or "no tags suggested", severity="warning")

    def action_undo(self) -> None:
        msg = transfer.undo(self.index)
        self.notify(msg, timeout=6)
        self.reload_rows()

    def action_help(self) -> None:
        self.push_screen(ConfirmDialog(
            "A small guide to ferry",
            "PICK UP A CONVERSATION\n"
            "↑ ↓       Navigate conversations\n"
            "enter     Open in its agent\n"
            "tab       Carry it to another agent\n"
            "2         Ask another agent for a second opinion\n\n"
            "FIND YOUR WAY\n"
            "/         Search titles and messages\n"
            "p         Read the preview · p again to return\n"
            "space     Select several conversations\n"
            "e         Expand the conversation's history\n"
            "g         Group by tag\n\n"
            "MAKE YOURSELF AT HOME\n"
            "r rename   t tags   d archive   u undo\n"
            "q quit     esc clear selection / go back",
            keys="enter or esc to return",
        ))


def run_tui() -> None:
    FerryApp().run()
