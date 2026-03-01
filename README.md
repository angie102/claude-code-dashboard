# Claude Code Companion Dashboard

A real-time TUI (Terminal User Interface) dashboard for monitoring [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions, agent teams, and subagents.

Built with [Textual](https://textual.textualize.io/).

## Why?

Claude Code's [Agent Teams](https://docs.anthropic.com/en/docs/claude-code/agent-teams) feature uses tmux for teammate visibility on macOS/Linux, but **tmux is not available on Windows**. This dashboard was built as a cross-platform alternative that provides full team monitoring without tmux — teammate conversations, task progress, subagent status, and more — all in a single companion terminal pane.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

- **Session Tabs** — Each active Claude Code session gets its own tab, auto-detected from JSONL files
- **API Quota Display** — Real-time 5-hour and 7-day usage bars (requires [claude-hud](https://github.com/mrpbennett/claude-hud) plugin)
- **Team Monitoring** — Team structure, members, and task progress tracking
- **Agent Tracking** — Live subagent status with spinner animation and elapsed time
- **Teammate Panel** — Full conversation flow between leader and teammates
  - Leader instructions (spawn, messages, shutdown requests)
  - Teammate reports with body preview
  - Nested subagent detection (`◐` running / `✓` completed)
- **Keyboard Navigation** — Arrow keys for tab switching, `r` to refresh, `d` for dark mode toggle

## Requirements

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and running
- [Textual](https://textual.textualize.io/) library

## Installation

```bash
pip install textual
```

Copy `dashboard.py` to any directory, or clone this repo:

```bash
git clone https://github.com/angie102/claude-code-dashboard.git
cd claude-code-dashboard
```

## Usage

### Basic

```bash
python dashboard.py
```

Automatically detects active Claude Code sessions (modified within the last 60 seconds).

### Options

```bash
# Custom threshold (detect sessions active within last 2 minutes)
python dashboard.py --threshold 120

# Open as a right split pane in Windows Terminal
python dashboard.py --split

# Open as a top split pane in Windows Terminal
python dashboard.py --top
```

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `←` `→` | Switch session tabs |
| `r` | Force refresh |
| `d` | Toggle dark mode |
| `q` | Quit |

## Layout

```
┌──────────────────────────────────────────┐
│ Usage │ 5h: ██░░░░░░░░ 25% │ 7d: ...    │  Quota Header
├──────────┬──────────────┬────────────────┤
│ #1       │ #2           │                │  Session Tabs
├──────────┴──────────────┴────────────────┤
│ ▸ latest user message                    │
│                                          │
│ ▸ Team: my-team (3 members)              │  Team Panel
│   Members                                │
│     team-lead (team-lead)                │
│     researcher (general-purpose)         │
│   Tasks ✓2 ◐1 ○2                        │
│                                          │
│ ◐ Agent [Explore] 15s                    │  Agent Panel
│   Finding auth code                      │
│                                          │
│ ▸ researcher                             │  Teammate Panel
│   16:48 LEAD ▸ spawned task              │
│     ◐ Explore finding code               │
│     ✓ Explore finding code               │
│   16:50 researcher analysis complete     │
│     ## Results...                         │
│   16:51 LEAD confirmed                   │
├──────────────────────────────────────────┤
│ 1 team │ 2 agents          16:52:30      │  Status Bar
└──────────────────────────────────────────┘
```

## How It Works

The dashboard reads Claude Code's internal files to display real-time status:

| Data | Source |
|------|--------|
| Sessions | `~/.claude/projects/*/*.jsonl` (JSONL modification time) |
| Agents | `tool_use` blocks with `name: "Agent"` in session JSONL |
| Teams | `~/.claude/teams/*/config.json` |
| Tasks | `~/.claude/tasks/*/*.json` |
| Teammates | `<teammate-message>` tags in session JSONL |
| Nested subagents | `{session_id}/subagents/*.jsonl` |
| Quota | `~/.claude/plugins/claude-hud/.usage-cache.json` |

All reads are **incremental** — the dashboard tracks file positions and only reads new content, keeping CPU and I/O usage minimal.

## Optional: claude-hud Plugin

The quota display at the top requires the [claude-hud](https://github.com/mrpbennett/claude-hud) plugin. Without it, the quota bar will be empty but everything else works fine.

## License

MIT
