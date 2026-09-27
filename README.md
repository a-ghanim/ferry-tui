# ferry

A little boat for your AI coding conversations. Search your local history, pick up where you left off, or carry a conversation from one agent to another.

![Ferry using entirely fictional conversations](https://raw.githubusercontent.com/a-ghanim/ferry-tui/main/demo/ferry.gif)

## Install

```sh
uv tool install ferry-tui
ferry
```

Requires Python 3.10 or newer and an existing supported agent store. Ferry is an early release, tested on macOS. The receiving agent's CLI must be installed and authenticated to continue a transferred conversation.

Already installed from PyPI? Run `uv tool upgrade ferry-tui` to get the latest release. If you used the earlier Git-tag install command, run `uv tool install --force ferry-tui` once to switch to PyPI releases.

## Use

| Key | Action |
| --- | --- |
| Enter | Open the selected conversation in its agent |
| Tab | Transfer context to another agent, then open it |
| / | Search titles and message text |
| p | Focus the transcript; switch panes in a narrow terminal |
| e | Expand linked sessions in a conversation |
| Space | Select multiple conversations |
| r / t | Rename / edit tags |
| d / u | Archive / undo the last Ferry write |
| 2 | Create a second opinion in the other available agents |
| q | Quit |

The transfer dialog shows the source, message and token estimates, target agent, and working folders. Choose raw context, compact context without tool calls/results/reasoning, or a brief summary. Ferry groups the resulting sessions together and marks where the context came from. A transfer carries recorded conversation text and available artifacts; it does not preserve a model's hidden state, running tools, permissions, or unsaved files.

## CLI and desktop behavior

Existing native Codex threads open in the desktop app on macOS, with CLI fallback where no desktop opener is available. Ferry-generated Codex sessions use `codex resume`: the synthetic history used by this adapter is not reliably displayed as prior chat bubbles in the desktop app. Ferry refuses to open these through the app when the CLI is missing. There is currently no UI/CLI preference toggle.

Claude Code opens with `claude --remote-control --resume`. Remote Control availability depends on Claude Code and your account. Cowork sessions are read-only; transfer their context to a writable agent to continue.

## Local storage and privacy

Ferry imports [Jackson Clark's handoff](https://github.com/HacksonClark/handoff) as a Python library. Its extractors and injectors convert agent records through a canonical transcript. Ferry adds search, titles, linked session history, transfer checks, and the interface.

| Store | Read from | Transfer writes |
| --- | --- | --- |
| Codex | `~/.codex/sessions/` and `session_index.jsonl` | New rollout; SQLite registration when the agent database exists |
| Claude Code, including desktop Code sessions in this store | `~/.claude/projects/` | New session under the selected folder |
| Claude Cowork | macOS Claude local-agent-mode-sessions store | Read-only |
| OpenCode | handoff's configured OpenCode store | Through handoff's adapter |

The searchable user/assistant text stays in `~/.cache/ferry/index.db`. There is no hosted Ferry backend. The cache is sensitive even after automatic secret redaction; redaction is best-effort and does not anonymize conversations. `ferry exclude <handle>` removes a conversation from searchable text. Archive and undo files also live in this directory, so do not delete the cache if you need those files back.

Ferry preserves transfer sources, refuses recently modified sessions, redacts known secret patterns, checks that the target adapter can read the new session, and logs writes for `ferry undo`. A successful adapter check is not a guarantee of compatibility with every future agent version.

Missing titles can be generated through your authenticated `claude -p`, which sends excerpts to Claude. Brief summaries and requested tag suggestions also use Claude. Normal indexing/search does not need a model. Use `FERRY_OFFLINE=1 ferry` to disable these model-assisted features and use local fallback labels/summaries.

## MCP

```sh
ferry mcp          # local stdio transport
ferry mcp-config   # client configuration examples
```

The read-only tools are `search_conversations` and `get_conversation`. Results label retrieved transcripts as data, not instructions. Configure this server only in clients you intend to give archive access. Optional `ferry mcp --http 8765` binds to localhost; do not expose it publicly without a separate authenticated access layer.

## Development

```sh
uv sync --extra dev
uv run pytest -q
uv build
```

Public tests construct fictional transcripts in temporary stores. No personal conversation fixtures or previous private Git history are included. `demo/make_gif.py` records the real UI and a verified transfer using fictional data; agent launch is disabled, and the transfer is undone. It needs macOS Quick Look and ffmpeg.

## Credits

handoff supplies extraction, injection, redaction, and its plugin registry. Ferry's Cowork reader registers through that plugin API. Design influences include claude-codex-bridge (lineage), session-porter (transfer modes), showagent (transfer previews), CASR (read-back checks), cct (folder remapping), Waybill, and syne. Ferry is independently maintained and is not an official OpenAI or Anthropic product.

MIT.
