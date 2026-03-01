#!/usr/bin/env python3
"""
Claude Code Companion Dashboard

세션별 companion panel로 동작하는 팀/에이전트 모니터 TUI.
- 세션 context 사용률 실시간 표시
- 팀 구조 (리더/팀원) + 태스크 진행 상황 추적
- subagent 실행 상태 모니터링

Usage:
    python dashboard.py                    # 최근 세션 자동 감지
    python dashboard.py --threshold 120    # 2분 이내 세션 감지
    python dashboard.py --split            # WT 우측 pane으로 분할 실행
    python dashboard.py --top              # WT 상단 pane으로 분할 실행
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static, TabbedContent, TabPane


# ── 설정 ────────────────────────────────────────────────────────

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
TEAMS_DIR = HOME / ".claude" / "teams"
TASKS_DIR = HOME / ".claude" / "tasks"
ACTIVE_THRESHOLD = 60  # 기본 60초
USAGE_CACHE = HOME / ".claude" / "plugins" / "claude-hud" / ".usage-cache.json"


def read_usage_cache() -> dict | None:
    """HUD usage cache 읽기. {fiveHour, sevenDay, planName, ...}"""
    try:
        if USAGE_CACHE.exists():
            return json.loads(USAGE_CACHE.read_text(encoding="utf-8")).get("data")
    except Exception:
        pass
    return None


BAR_WIDTH = 10


# ── HUD 색상 체계 (플러그인 소스 기준) ────────────────────────

def quota_color(pct: int) -> str:
    """API quota 사용률 색상. HUD getQuotaColor 기준: 90 red, 75 magenta, else blue."""
    if pct >= 90:
        return "#D4644E"
    if pct >= 75:
        return "#bd93f9"
    return "#8be9fd"


def _bar(pct: int, color_fn) -> str:
    """HUD 스타일 블록 바. █ filled + ░ empty."""
    safe = min(max(pct, 0), 100)
    filled = round(safe / 100 * BAR_WIDTH)
    empty = BAR_WIDTH - filled
    color = color_fn(pct)
    return f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/]"


def quota_bar(pct: int) -> str:
    return _bar(pct, quota_color)


# ── 활성 세션 감지 ──────────────────────────────────────────────

def find_live_sessions() -> list[Path]:
    """최근 ACTIVE_THRESHOLD초 이내 수정된 .jsonl = 현재 실행 중인 세션.

    프로젝트당 가장 최근 JSONL 하나만 반환하여,
    세션 continuation 시 중복 탭 방지.
    """
    now = time.time()
    by_project: dict[str, tuple[float, Path]] = {}
    if PROJECTS_DIR.exists():
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for jf in proj.glob("*.jsonl"):
                try:
                    mt = jf.stat().st_mtime
                    if (now - mt) < ACTIVE_THRESHOLD:
                        pkey = str(proj)
                        if pkey not in by_project or mt > by_project[pkey][0]:
                            by_project[pkey] = (mt, jf)
                except OSError:
                    pass
    result = sorted(by_project.values(), key=lambda x: x[0], reverse=True)
    return [p for _, p in result]


def scan_session_incremental(
    session_path: Path,
    state: dict,
) -> tuple[list[dict], list[str]]:
    """한 세션의 JSONL을 incremental 스캔.

    state keys: file_pos, agent_uses, agent_results
    Returns: (running_agents, newly_created_team_names)
    """
    agent_uses: dict[str, dict] = state["agent_uses"]
    agent_results: set[str] = state["agent_results"]
    new_teams: list[str] = []

    try:
        sz = session_path.stat().st_size
        last = state.get("file_pos", 0)
        if sz <= last:
            if sz < last:
                state["file_pos"] = 0
                last = 0
            else:
                running = [v for k, v in agent_uses.items() if k not in agent_results]
                return running, []

        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(last)
            new_content = f.read()
            state["file_pos"] = f.tell()

        for line in new_content.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = e.get("message", {})
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            ts = e.get("timestamp", "")
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name == "Agent":
                        tid = block.get("id", "")
                        inp = block.get("input", {})
                        agent_uses[tid] = {
                            "id": tid,
                            "description": inp.get("description", ""),
                            "subagent_type": inp.get("subagent_type", ""),
                            "background": inp.get("run_in_background", False),
                            "timestamp": ts,
                        }
                    elif name == "TeamCreate":
                        inp = block.get("input", {})
                        team_name = inp.get("team_name", "")
                        if team_name:
                            new_teams.append(team_name)
                elif block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid in agent_uses:
                        agent_results.add(tid)
    except Exception:
        pass

    running = [v for k, v in agent_uses.items() if k not in agent_results]
    return running, new_teams


# ── 팀 감지 ────────────────────────────────────────────────────

def find_active_teams() -> list[dict]:
    """~/.claude/teams/ 에서 팀 설정을 읽어 반환."""
    teams = []
    if not TEAMS_DIR.exists():
        return teams
    for td in TEAMS_DIR.iterdir():
        if not td.is_dir():
            continue
        cfg = td / "config.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                data["_name"] = td.name
                teams.append(data)
            except Exception:
                pass
    return teams


def read_tasks(team_name: str) -> list[dict]:
    """팀의 태스크 목록을 읽어 반환."""
    tasks = []
    task_dir = TASKS_DIR / team_name
    if not task_dir.exists():
        return tasks
    for tf in sorted(task_dir.glob("*.json")):
        try:
            tasks.append(json.loads(tf.read_text(encoding="utf-8")))
        except Exception:
            pass
    return tasks


# ── JSONL 유틸 ──────────────────────────────────────────────────

def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text"
            else b if isinstance(b, str) else ""
            for b in content
        )
    return ""


def extract_session_title(path: Path, max_chars: int = 40) -> str:
    """JSONL의 첫 사용자 메시지에서 세션 제목 추출.

    system-reminder, XML 태그 제거 후 앞 max_chars자.
    못 찾으면 session ID 앞 12자로 fallback.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = e.get("message", {})
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                text = _text(msg.get("content", ""))
                text = re.sub(
                    r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL
                )
                text = re.sub(r"<[^>]+>", "", text)
                text = text.strip()
                if text:
                    title = text.split("\n")[0].strip()[:max_chars]
                    if title:
                        return title
    except Exception:
        pass
    return path.stem[:12]


def extract_latest_user_message(path: Path, max_chars: int = 80) -> str:
    """JSONL의 마지막 사용자 메시지를 추출 (파일 끝에서 역순 탐색)."""
    try:
        size = path.stat().st_size
        read_size = min(size, 100_000)  # 마지막 100KB만
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > read_size:
                f.seek(size - read_size)
            lines = f.readlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = e.get("message", {})
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            text = _text(msg.get("content", ""))
            text = re.sub(
                r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL
            )
            text = re.sub(
                r"<teammate-message\s[^>]*>.*?</teammate-message>",
                "", text, flags=re.DOTALL,
            )
            text = re.sub(r"<[^>]+>", "", text)
            text = text.strip()
            if text:
                return text.split("\n")[0].strip()[:max_chars]
    except Exception:
        pass
    return ""


# ── 팀원 메시지 파싱 ─────────────────────────────────────────────

_TEAMMATE_MSG_RE = re.compile(
    r"<teammate-message\s+([^>]+)>(.*?)</teammate-message>", re.DOTALL
)


def parse_line_for_teammates(line: str) -> dict[str, list[str]]:
    """JSONL 한 줄에서 팀원별 메시지를 추출. {teammate_id: [formatted_str, ...]} 반환."""
    try:
        e = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return {}

    msg = e.get("message", {})
    if not isinstance(msg, dict):
        return {}

    role = msg.get("role", "")
    content = msg.get("content", "")
    ts = e.get("timestamp", "")[11:16]
    results: dict[str, list[str]] = {}

    if role == "user":
        text = _text(content)
        for match in _TEAMMATE_MSG_RE.finditer(text):
            attrs_str, body = match.group(1), match.group(2).strip()
            tid_m = re.search(r'teammate_id="([^"]+)"', attrs_str)
            if not tid_m:
                continue
            tid = tid_m.group(1)
            sum_m = re.search(r'summary="([^"]*)"', attrs_str)
            summary = sum_m.group(1) if sum_m else ""

            # system 메시지 (terminated 등)
            if tid == "system":
                try:
                    data = json.loads(body)
                    who = re.search(r"(\w+) has shut down", data.get("message", ""))
                    if who:
                        results.setdefault(who.group(1), []).append(
                            f"[#6A6158]{ts}[/] [#D4644E]terminated[/]"
                        )
                except (json.JSONDecodeError, ValueError):
                    pass
                continue

            # JSON 프로토콜 (idle, shutdown_approved 등)
            try:
                data = json.loads(body)
                msg_type = data.get("type", "")
                if msg_type == "idle_notification":
                    continue  # idle 노이즈 숨김
                elif msg_type == "shutdown_approved":
                    results.setdefault(tid, []).append(
                        f"[#6A6158]{ts}[/] [#D4644E]shutdown[/]"
                    )
                else:
                    results.setdefault(tid, []).append(
                        f"[#6A6158]{ts}[/] [dim]{msg_type}[/]"
                    )
                continue
            except (json.JSONDecodeError, ValueError):
                pass

            # 일반 텍스트 메시지 — summary + body 표시
            lines: list[str] = []
            if summary:
                lines.append(
                    f"[#6A6158]{ts}[/] [bold #6BAF78]{tid}[/] {summary}"
                )
            body_clean = body.strip()
            if body_clean:
                for bline in body_clean.split("\n")[:12]:
                    bl = bline.strip()
                    if bl:
                        lines.append(f"  [#9A9389]{bl[:100]}[/]")
            if not lines:
                lines.append(
                    f"[#6A6158]{ts}[/] [bold #6BAF78]{tid}[/] (message)"
                )
            results.setdefault(tid, []).extend(lines)

    elif role == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name", "")
            inp = b.get("input", {})
            if name == "SendMessage":
                recipient = inp.get("recipient", "")
                if not recipient:
                    continue
                msg_type = inp.get("type", "message")
                msg_summary = inp.get("summary", "")
                msg_content = inp.get("content", "")
                if msg_type == "shutdown_request":
                    results.setdefault(recipient, []).append(
                        f"[#6A6158]{ts}[/] [bold #D97757]LEAD[/]"
                        f" \u25cf shutdown request"
                    )
                else:
                    header = msg_summary or msg_content[:80]
                    results.setdefault(recipient, []).append(
                        f"[#6A6158]{ts}[/] [bold #D97757]LEAD[/] {header}"
                    )
                    if msg_content and msg_content != header:
                        for cl in msg_content.split("\n")[:6]:
                            cl = cl.strip()
                            if cl:
                                results.setdefault(recipient, []).append(
                                    f"  [#9A9389]{cl[:100]}[/]"
                                )
            elif name == "Agent":
                mate_name = inp.get("name", "")
                team = inp.get("team_name", "")
                if mate_name and team:
                    desc = inp.get("description", mate_name)
                    results.setdefault(mate_name, []).append(
                        f"[#6A6158]{ts}[/] [bold #D97757]LEAD[/]"
                        f" \u25b8 {desc}"
                    )

    return results


# ── 위젯: QuotaHeader ─────────────────────────────────────────

class QuotaHeader(Static):
    """API quota 사용률 1줄 표시."""

    DEFAULT_CSS = """
    QuotaHeader {
        dock: top;
        height: 1;
        background: transparent;
        color: #E8E6E3;
        padding: 0 1;
    }
    """

    def on_mount(self):
        self._refresh()
        self.set_interval(60.0, self._refresh)

    def _refresh(self):
        usage = read_usage_cache()
        if usage:
            five = usage.get("fiveHour", 0)
            seven = usage.get("sevenDay", 0)
            fc, sc = quota_color(five), quota_color(seven)
            parts = [
                "[bold #D97757]Claude Dashboard[/]",
                f"5h: {quota_bar(five)} [{fc}]{five}%[/]",
                f"7d: {quota_bar(seven)} [{sc}]{seven}%[/]",
            ]
            self.update(" \u2502 ".join(parts))
        else:
            self.update("[bold #D97757]Claude Dashboard[/]")


# ── 위젯: 상태바 ───────────────────────────────────────────────

class StatusBar(Static):
    """하단 상태바 — 시간, 팀, 에이전트 수."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: transparent;
        color: #6BAF78;
        padding: 0 1;
    }
    """

    def on_mount(self):
        self.set_interval(1.0, self._tick)
        self._tick()

    def _tick(self):
        app = self.app
        teams = getattr(app, "active_teams", [])
        sessions = getattr(app, "_sessions", {})
        running = sum(
            1 for state in sessions.values()
            for p in state.get("known_agents", {}).values()
            if not p._completed
        )

        parts: list[str] = []
        if teams:
            parts.append(f"[#C4A584]{len(teams)} team[/]")
        if running:
            parts.append(f"[#E8E6E3]{running} agents[/]")
        if parts:
            self.display = True
            self.update("  \u2502  ".join(parts))
        else:
            self.display = False


# ── 위젯: 팀 패널 ──────────────────────────────────────────────

class TeamPanel(Vertical):
    """팀 구조 + 태스크 목록 표시."""

    DEFAULT_CSS = """
    TeamPanel {
        height: auto;
        max-height: 14;
        background: transparent;
        border: round #C15F3C;
        margin: 0 0 1 0;
    }
    TeamPanel .team-header {
        dock: top;
        height: 1;
        background: #C15F3C;
        color: #E8E6E3;
        text-style: bold;
        padding: 0 1;
    }
    TeamPanel RichLog {
        height: auto;
        max-height: 12;
        background: transparent;
        overflow-x: hidden;
        scrollbar-size: 1 1;
        scrollbar-background: transparent;
        scrollbar-color: #3A352B;
    }
    """

    def __init__(self, team_name: str, **kwargs):
        super().__init__(**kwargs)
        self.team_name = team_name

    def compose(self) -> ComposeResult:
        yield Static(f" \u25b8 Team: {self.team_name} ", classes="team-header")
        yield RichLog(highlight=True, markup=True, wrap=True)

    def on_mount(self):
        self._refresh()
        self.set_interval(3.0, self._refresh)

    def _refresh(self):
        log = self.query_one(RichLog)
        log.clear()

        # 팀 멤버
        teams = find_active_teams()
        team = next((t for t in teams if t["_name"] == self.team_name), None)
        if team:
            members = team.get("members", [])
            log.write("  [bold #9A9389]Members[/]")
            for m in members:
                name = m.get("name", "?")
                atype = m.get("agentType", "")
                log.write(f"    [bold #D97757]{name}[/] [dim]({atype})[/]")

        # 태스크 목록
        tasks = read_tasks(self.team_name)
        if tasks:
            done = sum(1 for t in tasks if t.get("status") == "completed")
            prog = sum(1 for t in tasks if t.get("status") == "in_progress")
            pend = sum(1 for t in tasks if t.get("status") == "pending")
            log.write(
                f"  [bold #9A9389]Tasks[/]"
                f" [#6BAF78]\u2713{done}[/]"
                f" [#C4A584]\u2192{prog}[/]"
                f" [#6A6158]\u00b7{pend}[/]"
            )
            for t in tasks:
                status = t.get("status", "pending")
                icons = {
                    "completed": "[#6BAF78]\u2713[/]",
                    "in_progress": "[#C4A584]\u2192[/]",
                    "pending": "[#6A6158]\u00b7[/]",
                }
                icon = icons.get(status, "?")
                owner = t.get("owner", "")
                owner_str = f" [dim]({owner})[/]" if owner else ""
                subj = t.get("subject", "?")[:45]
                log.write(f"    {icon} {subj}{owner_str}")
        else:
            log.write("  [dim]No tasks yet[/]")


# ── 위젯: 에이전트 패널 ──────────────────────────────────────────

AGENT_SPINNERS = ["\u25d0", "\u25d3", "\u25d1", "\u25d2"]


class AgentPanel(Vertical):
    """실행 중인 subagent 상태 표시 패널."""

    DEFAULT_CSS = """
    AgentPanel {
        border: round #3A352B;
        height: auto;
        min-height: 3;
        max-height: 10;
        background: transparent;
    }
    AgentPanel .agent-title {
        dock: top;
        height: 1;
        background: #2A2520;
        color: #C4A584;
        text-style: bold;
        padding: 0 1;
    }
    AgentPanel .agent-info {
        height: auto;
        padding: 0 1;
        color: #9A9389;
    }
    """

    def __init__(self, agent_id: str, description: str, subagent_type: str,
                 start_time: str, **kwargs):
        super().__init__(**kwargs)
        self.agent_id = agent_id
        self.description = description
        self.subagent_type = subagent_type
        self.start_time = start_time
        self._tick = 0
        self._completed = False

    def compose(self) -> ComposeResult:
        spinner = AGENT_SPINNERS[0]
        type_str = f" [{self.subagent_type}]" if self.subagent_type else ""
        yield Static(
            f" {spinner} Agent{type_str} ",
            classes="agent-title",
            id=f"atitle-{self.agent_id[:12]}",
        )
        yield Static(
            f" {self.description}",
            classes="agent-info",
            id=f"ainfo-{self.agent_id[:12]}",
        )

    def on_mount(self):
        self.set_interval(0.5, self._update_spinner)

    def _update_spinner(self):
        if self._completed:
            return
        self._tick += 1
        spinner = AGENT_SPINNERS[self._tick % len(AGENT_SPINNERS)]
        type_str = f" [{self.subagent_type}]" if self.subagent_type else ""
        elapsed = self._elapsed_str()
        try:
            title = self.query_one(f"#atitle-{self.agent_id[:12]}", Static)
            title.update(f" {spinner} Agent{type_str} {elapsed} ")
        except Exception:
            pass

    def _elapsed_str(self) -> str:
        """시작 시간부터 경과 시간 계산."""
        if not self.start_time:
            return ""
        try:
            start = datetime.fromisoformat(self.start_time.replace("Z", "+00:00"))
            now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
            delta = int((now - start).total_seconds())
            if delta < 60:
                return f"{delta}s"
            return f"{delta // 60}m {delta % 60}s"
        except Exception:
            return ""

    def mark_completed(self):
        """에이전트 완료 표시."""
        self._completed = True
        type_str = f" [{self.subagent_type}]" if self.subagent_type else ""
        elapsed = self._elapsed_str()
        try:
            title = self.query_one(f"#atitle-{self.agent_id[:12]}", Static)
            title.update(f" \u2713 Agent{type_str} {elapsed} ")
            title.styles.color = "#6BAF78"
        except Exception:
            pass


# ── 위젯: 팀원 패널 ─────────────────────────────────────────────

TEAMMATE_PALETTE = ["#6BAF78", "#7EA8BE", "#A68CB3", "#C4847A", "#C4A584"]
_teammate_color_map: dict[str, str] = {}


def _teammate_color(name: str) -> str:
    if name not in _teammate_color_map:
        idx = len(_teammate_color_map) % len(TEAMMATE_PALETTE)
        _teammate_color_map[name] = TEAMMATE_PALETTE[idx]
    return _teammate_color_map[name]


class TeammatePanel(Vertical):
    """리더 JSONL에서 특정 팀원의 메시지만 필터링하여 표시."""

    DEFAULT_CSS = """
    TeammatePanel {
        border: round #3A352B;
        min-height: 6;
        height: 1fr;
        background: transparent;
    }
    TeammatePanel .panel-title-teammate {
        dock: top;
        height: 1;
        background: transparent;
        color: #E8E6E3;
        text-style: bold;
        padding: 0 1;
    }
    TeammatePanel RichLog {
        height: 1fr;
        background: transparent;
        overflow-x: hidden;
        scrollbar-size: 1 1;
        scrollbar-background: transparent;
        scrollbar-color: #3A352B;
        scrollbar-color-hover: #6A6158;
        scrollbar-color-active: #6BAF78;
    }
    """

    def __init__(self, teammate_id: str, session_path: Path | None = None, **kwargs):
        super().__init__(**kwargs)
        self.teammate_id = teammate_id
        self.session_path = session_path
        self._file_positions: dict[str, int] = {}
        # 중첩 subagent 추적
        self._own_jsonl: Path | None = None
        self._own_file_pos: int = 0
        self._subagent_uses: dict[str, dict] = {}
        self._subagent_results: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Static(
            f" \u25b8 {self.teammate_id} ",
            classes="panel-title-teammate",
        )
        yield RichLog(highlight=True, markup=True, wrap=True)

    def on_mount(self):
        self._load_initial()
        self.set_interval(0.5, self._poll)

    def _get_jsonl_files(self) -> list[Path]:
        """최근 활성 JSONL 세션 파일 목록."""
        files = []
        if PROJECTS_DIR.exists():
            for proj in PROJECTS_DIR.iterdir():
                if not proj.is_dir():
                    continue
                for jf in proj.glob("*.jsonl"):
                    try:
                        if (time.time() - jf.stat().st_mtime) < 600:
                            files.append(jf)
                    except OSError:
                        pass
        return files

    def _load_initial(self):
        log = self.query_one(RichLog)
        entries: list[str] = []
        for jf in self._get_jsonl_files():
            try:
                with open(jf, "r", encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        results = parse_line_for_teammates(raw)
                        if self.teammate_id in results:
                            entries.extend(results[self.teammate_id])
                    self._file_positions[str(jf)] = f.tell()
            except Exception:
                pass
        if entries:
            for e in entries[-30:]:
                log.write(e)
        else:
            log.write(
                f"[dim #6A6158]waiting for {self.teammate_id}...[/]"
            )

    def _find_own_jsonl(self) -> Path | None:
        """subagents/ 디렉토리에서 이 팀원의 JSONL 파일을 찾는다."""
        if self._own_jsonl is not None:
            return self._own_jsonl
        if not self.session_path:
            return None
        subagents_dir = (
            self.session_path.parent / self.session_path.stem / "subagents"
        )
        if not subagents_dir.exists():
            return None
        for jf in subagents_dir.glob("*.jsonl"):
            try:
                with open(jf, "r", encoding="utf-8", errors="replace") as f:
                    first_line = f.readline()
                if f'"{self.teammate_id}"' in first_line:
                    self._own_jsonl = jf
                    return jf
            except Exception:
                pass
        return None

    def _poll_subagents(self):
        """팀원 JSONL에서 Agent tool_use 블록을 감지하여 중첩 표시."""
        own = self._find_own_jsonl()
        if not own:
            return
        try:
            sz = own.stat().st_size
            if sz <= self._own_file_pos:
                if sz < self._own_file_pos:
                    self._own_file_pos = 0
                return
            with open(own, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._own_file_pos)
                new_content = f.read()
                self._own_file_pos = f.tell()
            log = self.query_one(RichLog)
            for line in new_content.splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = e.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if (block.get("type") == "tool_use"
                            and block.get("name") == "Agent"):
                        tid = block.get("id", "")
                        inp = block.get("input", {})
                        self._subagent_uses[tid] = {
                            "description": inp.get("description", ""),
                            "subagent_type": inp.get("subagent_type", ""),
                        }
                        desc = inp.get("description", "agent")
                        stype = inp.get("subagent_type", "")
                        log.write(
                            f"  [#6A6158]\u25d0[/]"
                            f" [bold #D97757]{stype}[/] {desc}"
                        )
                    elif block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "")
                        if (tid in self._subagent_uses
                                and tid not in self._subagent_results):
                            self._subagent_results.add(tid)
                            info = self._subagent_uses[tid]
                            desc = info.get("description", "agent")
                            stype = info.get("subagent_type", "")
                            log.write(
                                f"  [#6A6158]\u2713[/]"
                                f" [bold #6BAF78]{stype}[/] {desc}"
                            )
        except Exception:
            pass

    def _poll(self):
        log = self.query_one(RichLog)
        for jf in self._get_jsonl_files():
            key = str(jf)
            try:
                sz = jf.stat().st_size
                last = self._file_positions.get(key, 0)
                if sz <= last:
                    if sz < last:
                        self._file_positions[key] = 0
                    continue
                with open(jf, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(last)
                    new = f.read()
                    self._file_positions[key] = f.tell()
                for raw in new.splitlines():
                    if not raw.strip():
                        continue
                    results = parse_line_for_teammates(raw)
                    if self.teammate_id in results:
                        for entry in results[self.teammate_id]:
                            log.write(entry)
            except Exception:
                pass
        self._poll_subagents()


# ── 위젯: 세션 탭 ──────────────────────────────────────────────


class SessionPane(TabPane):
    """세션별 탭 패널. 최신 메시지 + 에이전트/팀원 컨테이너."""

    def __init__(self, title: str, *, pane_id: str):
        super().__init__(title, id=pane_id)
        self.pane_id = pane_id

    def compose(self) -> ComposeResult:
        with Vertical(id=f"content-{self.pane_id}", classes="session-content"):
            yield Static("", id=f"latest-{self.pane_id}", classes="session-latest")
            yield Vertical(id=f"agents-{self.pane_id}", classes="session-agents")
            yield Vertical(id=f"mates-{self.pane_id}", classes="session-mates")


# ── 앱 ─────────────────────────────────────────────────────────

class DashboardApp(App):
    TITLE = "Claude Dashboard"

    CSS = """
    Screen {
        background: transparent;
    }
    #sessions {
        height: 1fr;
    }
    .session-content {
        height: 1fr;
    }
    .session-latest {
        height: auto;
        padding: 0 1;
        color: #9A9389;
        background: transparent;
    }
    .session-agents {
        height: auto;
    }
    .session-mates {
        height: 1fr;
    }
    #splash {
        content-align: center middle;
        height: 1fr;
        color: #6A6158;
        text-align: center;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("d", "toggle_dark", "Dark/Light"),
        ("left", "prev_tab", "Prev"),
        ("right", "next_tab", "Next"),
    ]

    def __init__(self):
        super().__init__()
        self._sessions: dict[str, dict] = {}
        self._session_counter: int = 0
        self.active_teams: list[dict] = find_active_teams()

    def compose(self) -> ComposeResult:
        yield QuotaHeader()
        yield TabbedContent(id="sessions")
        yield Static(
            "\n\n[bold #6A6158]\u2b21 Claude Dashboard[/]\n\n"
            "[#6A6158]"
            "팀/에이전트 대기 중...\n\n"
            "Claude Code 세션에서 팀 작업을 시작하면 자동으로 감지합니다.\n\n"
            "[dim]r: 새로고침  q: 종료  d: 테마 전환[/][/]",
            id="splash",
        )
        yield StatusBar()

    def on_mount(self):
        tabs = self.query_one("#sessions", TabbedContent)
        tabs.display = False
        self.set_interval(3.0, self._scan_live)

    def _remove_splash(self):
        try:
            self.query_one("#splash").remove()
        except Exception:
            pass

    def _scan_live(self):
        """활성 세션을 감지하여 탭별 패널 업데이트."""
        session_paths = find_live_sessions()
        try:
            tabs = self.query_one("#sessions", TabbedContent)
        except Exception:
            return

        # 새 세션 탭 생성 (내부 mount는 다음 cycle에서 처리)
        new_session_keys: set[str] = set()
        for sp in session_paths:
            key = str(sp)
            if key not in self._sessions:
                pane_id = f"s-{sp.stem[:12]}"
                self._session_counter += 1
                tab_label = f"#{self._session_counter}"
                self._sessions[key] = {
                    "path": sp,
                    "title": tab_label,
                    "pane_id": pane_id,
                    "file_pos": 0,
                    "agent_uses": {},
                    "agent_results": set(),
                    "known_agents": {},
                    "known_teams": set(),
                    "known_teammates": set(),
                    "team_members": {},
                }
                self._remove_splash()
                tabs.display = True
                tabs.add_pane(SessionPane(tab_label, pane_id=pane_id))
                new_session_keys.add(key)

        # 기존 세션 업데이트 (방금 생성된 탭은 mount 완료까지 대기)
        for sp in session_paths:
            key = str(sp)
            if key in new_session_keys:
                continue
            state = self._sessions[key]
            pane_id = state["pane_id"]

            # Incremental 스캔
            old_pos = state["file_pos"]
            running_agents, new_team_names = scan_session_incremental(sp, state)

            # 새 내용이 추가된 경우에만 최신 사용자 메시지 갱신
            if state["file_pos"] != old_pos:
                try:
                    latest_w = self.query_one(f"#latest-{pane_id}", Static)
                    latest_msg = extract_latest_user_message(sp)
                    if latest_msg:
                        latest_w.update(f"[#9A9389]\u25b8[/] {latest_msg}")
                except Exception:
                    pass
            running_ids = {a["id"] for a in running_agents}

            # 새 에이전트 패널 추가
            try:
                agents_container = self.query_one(f"#agents-{pane_id}")
            except Exception:
                continue

            for agent in running_agents:
                aid = agent["id"]
                if aid not in state["known_agents"]:
                    panel = AgentPanel(
                        agent_id=aid,
                        description=agent["description"],
                        subagent_type=agent["subagent_type"],
                        start_time=agent["timestamp"],
                    )
                    state["known_agents"][aid] = panel
                    agents_container.mount(panel)

            # 완료된 에이전트 표시
            for aid, panel in state["known_agents"].items():
                if aid not in running_ids and not panel._completed:
                    panel.mark_completed()

            # TeamCreate로 감지된 팀 → 해당 세션 탭에 TeamPanel 추가
            for tname in new_team_names:
                if tname not in state["known_teams"]:
                    state["known_teams"].add(tname)
                    try:
                        content_c = self.query_one(f"#content-{pane_id}")
                        agents_c = self.query_one(f"#agents-{pane_id}")
                        content_c.mount(TeamPanel(tname), before=agents_c)
                    except Exception:
                        pass

        # 팀 멤버 → 해당 세션 탭에 TeammatePanel 추가 + 삭제된 팀 정리
        current_teams = find_active_teams()
        current_team_names = {t["_name"] for t in current_teams}
        self.active_teams = current_teams

        for state in self._sessions.values():
            pane_id = state["pane_id"]

            # 새 팀원 감지
            for team in current_teams:
                tname = team["_name"]
                if tname in state["known_teams"]:
                    members_set = state["team_members"].setdefault(tname, set())
                    for m in team.get("members", []):
                        mname = m.get("name", "")
                        if (mname
                                and m.get("agentType") != "team-lead"
                                and mname not in state["known_teammates"]):
                            state["known_teammates"].add(mname)
                            members_set.add(mname)
                            try:
                                mates_c = self.query_one(f"#mates-{pane_id}")
                                mates_c.mount(TeammatePanel(
                                    mname,
                                    session_path=state["path"],
                                ))
                            except Exception:
                                pass

            # 삭제된 팀 정리
            removed = state["known_teams"] - current_team_names
            for tname in removed:
                state["known_teams"].discard(tname)
                for panel in self.query("TeamPanel"):
                    if panel.team_name == tname:
                        panel.remove()
                members = state["team_members"].pop(tname, set())
                for mname in members:
                    state["known_teammates"].discard(mname)
                    for tp in self.query("TeammatePanel"):
                        if tp.teammate_id == mname:
                            tp.remove()

    def _switch_tab(self, direction: int):
        """탭을 direction 방향으로 전환. +1=다음, -1=이전."""
        try:
            tc = self.query_one("#sessions", TabbedContent)
            pane_ids = [state["pane_id"] for state in self._sessions.values()]
            if len(pane_ids) <= 1:
                return
            current = tc.active
            if current in pane_ids:
                idx = pane_ids.index(current)
                tc.active = pane_ids[(idx + direction) % len(pane_ids)]
        except Exception:
            pass

    def action_prev_tab(self):
        self._switch_tab(-1)

    def action_next_tab(self):
        self._switch_tab(1)

    def action_refresh(self):
        self._scan_live()

    def action_quit(self) -> None:
        self.exit()

    def action_toggle_dark(self):
        self.dark = not self.dark


# ── CLI ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Companion Dashboard",
    )
    parser.add_argument(
        "--threshold", "-t", type=int, default=60,
        help="활성 세션 감지 임계값 (초, 기본: 60)",
    )
    parser.add_argument(
        "--split", "-s", action="store_true",
        help="Windows Terminal 우측 pane으로 분할 실행",
    )
    parser.add_argument(
        "--top", action="store_true",
        help="Windows Terminal 상단 pane으로 분할 실행 (현재 터미널 위에 붙임)",
    )
    args = parser.parse_args()

    # --top / --split: Windows Terminal pane으로 분할 실행
    if args.top or args.split:
        script = Path(__file__).resolve()
        fwd_args = [a for a in sys.argv[1:] if a not in ("--split", "-s", "--top")]
        if args.top:
            cmd = ["wt", "-w", "0", "sp", "-H", "-s", "0.25", "python", str(script)] + fwd_args
            print("대시보드를 상단 pane으로 실행했습니다.", file=sys.stderr)
        else:
            cmd = ["wt", "-w", "0", "sp", "-V", "-s", "0.35", "python", str(script)] + fwd_args
            print("대시보드를 우측 pane으로 실행했습니다.", file=sys.stderr)
        subprocess.Popen(cmd)
        return

    global ACTIVE_THRESHOLD
    ACTIVE_THRESHOLD = args.threshold

    teams = find_active_teams()
    if teams:
        for t in teams:
            members = t.get("members", [])
            print(
                f"  \u25b8 Team: {t['_name']} ({len(members)} members)", file=sys.stderr
            )
    else:
        print("활성 팀 없음. 팀 작업 시작 시 자동 감지.", file=sys.stderr)

    app = DashboardApp()
    try:
        app.run(mouse=False)
    finally:
        # Ctrl+C 등 비정상 종료 시 터미널 상태 복원
        sys.stdout.write(
            "\x1b[?1000l"   # 마우스 클릭 트래킹 off
            "\x1b[?1003l"   # 마우스 모션 트래킹 off
            "\x1b[?1006l"   # SGR 마우스 모드 off
            "\x1b[?25h"     # 커서 표시
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
