"""대화 세션 2종 -- ``ChatBackend`` 하나 뒤의 두 구현 (REF/AGENT_PRODUCT.md "채팅 세션 2종").

② ``LocalBackend``: 로컬 Qwen(vLLM) + basic_agent react. 손눈서브 도구를 basic_agent 도구로 등록한다.
① ``ClaudeBackend``: Claude Code headless (``claude -p``). 기본 도구는 끄고(``--tools ""``) 손눈서브 MCP 도구만
   (mcp_bridge.py -> 서버 POST /api/tools/{name}). 개발용 CLAUDE.md·사용자 설정은 싣지 않는다(``--setting-sources ""``).
   보는 페이지 내용이 Anthropic 으로 나간다 -- 화면이 알린다.

둘 다 같은 이벤트를 낸다 (화면 SSE 가 그대로 받음):
  {"type": "text", "text"} · {"type": "tool", "name", "input"} · {"type": "tool_result", "name", "ok", "summary"}
  · {"type": "done", "text", "cost"?} · {"type": "error", "kind", "message"} · {"type": "session", "id"} (서비스만 씀)
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional, Protocol

from sonnun.tools import TOOLS, render_for_model, summarize

Backend = Literal["local", "claude"]
ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "assistant" / "GUIDE.md"
# basic_agent (https://github.com/Taeksu-Kim/basic_agent) — 이 저장소 옆에 두거나 BASIC_AGENT_PATH 로
BASIC_AGENT = Path(os.environ.get("BASIC_AGENT_PATH") or ROOT.parent / "basic_agent")
HISTORY_MESSAGES = 6                 # 로컬 LLM 에 다시 보여 줄 이전 메시지 수 (컨텍스트가 작다)


class Cancelled(BaseException):
    """사용자가 중지함. 도구·모델 호출 사이에서 던진다 -- ``except Exception`` 에 잡히지 않게 BaseException."""


@dataclass
class Turn:
    chat: dict                        # chats 행
    history: list[dict]               # 이전 메시지 (role, text)
    text: str                         # 이번 사용자 말
    cancel: threading.Event = field(default_factory=threading.Event)


class ChatBackend(Protocol):
    name: Backend

    def available(self) -> tuple[bool, str]: ...      # (쓸 수 있나, 못 쓰면 이유와 켜는 법)

    def run(self, turn: Turn) -> Iterator[dict]: ...


ToolCaller = Callable[[str, dict, str, str], dict]    # (도구, 인자, chat_id, source) -> 결과


# ---------------------------------------------------------------- ② 로컬 LLM (basic_agent react)
LOCAL_START = ("vLLM(:8002)을 켜 주세요 — side_projects/reference/LLM.md 2절. "
               "켠 뒤 서버를 다시 띄울 필요는 없습니다.")


class _CancellableLLM:
    """모델 호출 앞뒤로 중지 여부를 본다 (도구를 안 쓰고 생각만 하는 단계에서도 멈추게)."""

    def __init__(self, inner: Any, cancel: threading.Event) -> None:
        self.inner, self.cancel = inner, cancel

    def complete(self, system: str, user: str, *, schema: Optional[dict] = None) -> str:
        if self.cancel.is_set():
            raise Cancelled()
        out = self.inner.complete(system, user, schema=schema)
        if self.cancel.is_set():
            raise Cancelled()
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def history_query(turn: Turn) -> str:
    past = [m for m in turn.history if m.get("text")][-HISTORY_MESSAGES:]
    if not past:
        return turn.text
    lines = [f"{'사용자' if m['role'] == 'user' else '도우미'}: {m['text'][:300]}" for m in past]
    return "[이전 대화]\n" + "\n".join(lines) + f"\n\n[지금 요청]\n{turn.text}"


class LocalBackend:
    name: Backend = "local"

    def __init__(self, llm_factory: Callable[[], Any], ping: Callable[[], tuple[bool, str]], call_tool: ToolCaller,
                 *, guide: Path = GUIDE, max_steps: int = 10) -> None:
        self.llm_factory, self.ping, self.call_tool = llm_factory, ping, call_tool
        self.guide, self.max_steps = guide, max_steps

    def available(self) -> tuple[bool, str]:
        return self.ping()

    def _tool(self, name: str, turn: Turn, q: "queue.Queue") -> Callable[..., str]:
        def run(**kw: Any) -> str:
            if turn.cancel.is_set():
                raise Cancelled()
            q.put(("event", {"type": "tool", "name": name, "input": kw}))
            res = self.call_tool(name, kw, turn.chat["id"], "local")
            q.put(("event", {"type": "tool_result", "name": name, "ok": "error" not in res,
                             "summary": summarize(name, kw, res)}))
            if turn.cancel.is_set():
                raise Cancelled()
            return render_for_model(name, res)
        return run

    def run(self, turn: Turn) -> Iterator[dict]:
        if str(BASIC_AGENT) not in sys.path:
            sys.path.insert(0, str(BASIC_AGENT))
        from agent.core.context import ContextPolicy
        from agent.core.contract import envelope
        from agent.core.hitl import arun_graph
        from agent.core.llm import ContextOverflowError, LLMUnavailableError
        from agent.core.tools import ToolRegistry
        from agent.patterns import registry as patterns

        ok, why = self.available()
        if not ok:
            yield {"type": "error", "kind": "llm_down", "message": why}
            return
        q: queue.Queue = queue.Queue()
        reg = ToolRegistry()
        for spec in TOOLS.values():
            reg.register(spec.name, self._tool(spec.name, turn, q), description=spec.description,
                         args_schema=spec.schema, writes=spec.level == "hand")
        # 페이지 하나가 2천 자 안팎 -> 관측은 2,000자까지 그대로, 최근 2개만 남긴다 (8,192 토큰 컨텍스트).
        # keep_recent 는 basic_agent 에서 실모델 검증 전 옵션 (REF/AGENT.md) -- 이 프로젝트가 첫 사용처.
        policy = ContextPolicy(offload=True, inline_chars=2000, keep_recent=2)
        graph = patterns.get("react").build(llm=_CancellableLLM(self.llm_factory(), turn.cancel), registry=reg,
                                            max_steps=self.max_steps, system=self.guide.read_text(encoding="utf-8"),
                                            context_policy=policy)

        def work() -> None:
            try:
                q.put(("env", envelope(asyncio.run(arun_graph(graph, history_query(turn))))))
            except Cancelled:
                q.put(("cancelled", None))
            except ContextOverflowError as exc:
                q.put(("error", ("context_overflow", f"로컬 모델 컨텍스트(8,192 토큰)를 넘음: {str(exc)[:200]}")))
            except LLMUnavailableError as exc:
                q.put(("error", ("llm_down", f"로컬 LLM 연결이 끊김 ({exc}). {LOCAL_START}")))
            except Exception as exc:  # noqa: BLE001 -- 화면에 알리고 끝낸다
                q.put(("error", ("internal", f"{type(exc).__name__}: {exc}")))

        threading.Thread(target=work, daemon=True, name="sonnun-local-turn").start()
        while True:
            kind, val = q.get()
            if kind == "event":
                yield val
                continue
            if kind == "cancelled":
                yield {"type": "error", "kind": "cancelled", "message": "사용자가 중지함"}
            elif kind == "error":
                yield {"type": "error", "kind": val[0], "message": val[1]}
            else:
                env = val
                if env["status"] == "ok" and env["final"]:
                    yield {"type": "text", "text": env["final"]}
                    yield {"type": "done", "text": env["final"]}
                elif env["status"] == "capped":
                    msg = env["final"] or f"{self.max_steps}단계 안에 끝내지 못했습니다. 조금 더 구체적으로 말씀해 주세요."
                    yield {"type": "text", "text": msg}
                    yield {"type": "done", "text": msg}
                else:
                    err = env["error"] or "답을 만들지 못함"
                    if "context window" in err:
                        err = "로컬 모델 컨텍스트(8,192 토큰)를 넘었습니다. 대화를 새로 시작하거나 범위를 좁혀 주세요."
                    yield {"type": "error", "kind": "agent_error", "message": err}
            return


# ---------------------------------------------------------------- ① Claude Code
def run_subprocess(cmd: list[str], cwd: Path, cancel: threading.Event) -> Iterator[str]:
    """claude -p 를 띄워 stdout 줄을 내보낸다. 중지되거나 소비 쪽이 멈추면 프로세스를 끝낸다."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")

    def watch() -> None:
        cancel.wait()
        if proc.poll() is None:
            proc.terminate()

    threading.Thread(target=watch, daemon=True).start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line
        proc.wait(timeout=30)
        if proc.returncode and not cancel.is_set():
            err = (proc.stderr.read() if proc.stderr else "")[-400:]
            yield json.dumps({"type": "_exit", "code": proc.returncode, "stderr": err})
    finally:
        if proc.poll() is None:
            proc.terminate()


class ClaudeBackend:
    name: Backend = "claude"

    def __init__(self, server_url: str, *, runner: Optional[Callable[[list[str], Path, threading.Event], Iterator[str]]] = None,
                 claude_bin: Optional[str] = None, guide: Path = GUIDE) -> None:
        self.server_url = server_url.rstrip("/")
        self.runner = runner or run_subprocess
        self.bin = claude_bin if claude_bin is not None else shutil.which("claude")
        self.guide = guide
        self.home = guide.parent        # 세션 기록이 cwd 별로 저장되므로 늘 같은 곳에서 띄운다

    def available(self) -> tuple[bool, str]:
        if not self.bin:
            return False, "claude 명령이 없습니다 (Claude Code 설치·로그인 필요). 폐쇄망에서는 로컬 Qwen 만 쓸 수 있습니다"
        return True, ""

    def command(self, turn: Turn) -> list[str]:
        mcp = {"mcpServers": {"sonnun": {
            "command": sys.executable, "args": ["-m", "sonnun.mcp_bridge", "--url", self.server_url, "--chat", turn.chat["id"]],
            "env": {"PYTHONPATH": str(ROOT)}}}}
        first = not turn.chat.get("claude_started")
        return [self.bin or "claude", "-p", turn.text,
                "--session-id" if first else "--resume", turn.chat["claude_session"],
                "--tools", "", "--strict-mcp-config", "--mcp-config", json.dumps(mcp, ensure_ascii=False),
                "--allowedTools", "mcp__sonnun", "--setting-sources", "",
                "--append-system-prompt-file", str(self.guide),
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]

    def run(self, turn: Turn) -> Iterator[dict]:
        ok, why = self.available()
        if not ok:
            yield {"type": "error", "kind": "claude_down", "message": why}
            return
        names: dict[str, tuple[str, dict]] = {}
        streamed = False
        for line in self.runner(self.command(turn), self.home, turn.cancel):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                yield {"type": "session", "id": ev.get("session_id")}
            elif t == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    streamed = True
                    yield {"type": "text", "text": e["delta"]["text"]}
            elif t == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        name = str(c.get("name", "")).removeprefix("mcp__sonnun__")
                        names[c.get("id")] = (name, c.get("input") or {})
                        yield {"type": "tool", "name": name, "input": c.get("input") or {}}
                    elif c.get("type") == "text" and not streamed:
                        yield {"type": "text", "text": c.get("text", "")}
                streamed = False
            elif t == "user":
                for c in (ev.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        name, args = names.get(c.get("tool_use_id"), ("?", {}))
                        raw = c.get("content")
                        text = raw[0].get("text", "") if isinstance(raw, list) and raw else str(raw or "")
                        # mcp_bridge 가 결과 앞에 한 줄 요약을 붙여 보낸다 (도구 결과 원본은 모델용 글이라)
                        summary = text.split("\n", 1)[0].removeprefix("요약: ") if text.startswith("요약: ") else name
                        yield {"type": "tool_result", "name": name, "ok": not c.get("is_error"), "summary": summary}
            elif t == "result":
                if ev.get("is_error") or ev.get("subtype") != "success":
                    yield {"type": "error", "kind": "claude_error", "message": str(ev.get("result") or ev.get("subtype"))[:300]}
                    return
                yield {"type": "done", "text": ev.get("result", ""), "cost": ev.get("total_cost_usd")}
                return
            elif t == "_exit":
                yield {"type": "error", "kind": "claude_error",
                       "message": f"claude 가 오류로 끝남 (코드 {ev['code']}): {ev.get('stderr', '').strip()[-200:] or '로그인 상태 확인: claude'}"}
                return
        if turn.cancel.is_set():
            yield {"type": "error", "kind": "cancelled", "message": "사용자가 중지함"}
        else:
            yield {"type": "error", "kind": "claude_error", "message": "claude 가 결과 없이 끝남"}
