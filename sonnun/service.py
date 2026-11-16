"""서비스 -- 상태의 유일한 주인 (대화·메시지·활동 기록, 실행 중인 턴).

도구 실행은 세 창구가 모두 ``run_tool`` 하나를 거친다: 로컬 LLM 턴(스레드에서 ``run_tool_sync``) · Claude Code(MCP 다리 ->
HTTP) · import(HTTP). 그래서 활동 기록과 중지가 창구와 상관없이 한 곳에서 처리된다.
브라우저는 하나라서 대화 턴은 한 번에 하나만 돈다.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Iterator, Optional

from sonnun.bridge import BridgeTimeout, ExtensionBridge, NotConnected
from sonnun.chat import ChatBackend, Turn
from sonnun.store import Store
from sonnun.tools import TOOLS, check_args, summarize
from sonnun.vision import Vision


# 확장 답을 기다리는 시간: 사용자 승인(기본 120초, 설정에서 최대 600초)·사람에게 넘기기(최대 600초)·기다리기(최대 30초)가
# 끼는 동작은 길게 — 승인 대기보다 짧으면 사용자가 허용을 눌러도 서버가 먼저 포기한다
def _timeout(name: str, args: dict) -> float:
    if name == "ask_user":
        return min(600, max(10, int(args.get("timeout_sec") or 300))) + 15
    if name in ("click", "type_text", "press_key"):
        return 615
    if name == "wait_for":
        return 45
    return 40


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class SonnunService:
    def __init__(self, store: Store, bridge: ExtensionBridge) -> None:
        self.store, self.bridge = store, bridge
        self.backends: dict[str, ChatBackend] = {}
        self.vision: Optional[Vision] = None                       # look 도구 (로컬 비전 모델)
        self.loop: Optional[asyncio.AbstractEventLoop] = None     # 서버 이벤트 루프 (startup 에서 채움)
        self._lock = threading.Lock()
        self._running: Optional[tuple[str, threading.Event]] = None
        # 작업 탭: 대화 턴마다 사용자가 보낸 탭에 묶는다. 턴 도중 사용자가 다른 탭으로 옮겨도 에이전트는 그 탭에 머문다.
        # (확장은 tab_id 가 없으면 "지금 보고 있는 탭"을 쓰므로, 묶지 않으면 사용자가 보는 탭을 대신 조작하게 된다)
        self._work_tab: dict[str, int] = {}

    # ---------------------------------------------------------------- 도구
    async def run_tool(self, name: str, args: dict, *, chat_id: Optional[str] = None, source: str = "api") -> dict:
        if name not in TOOLS:
            return {"error": f"모르는 도구 {name} — 쓸 수 있는 도구: {', '.join(TOOLS)}", "code": "bad_op"}
        args = dict(args or {})
        problem = check_args(name, args)
        if problem:
            result: dict = {"error": problem, "code": "bad_args"}
        elif chat_id and self._cancelled(chat_id):
            result = {"error": "사용자가 중지함", "code": "cancelled"}
        else:
            op = "screenshot" if name == "look" else name
            op_args = {k: v for k, v in args.items() if k != "question"} if name == "look" else dict(args)
            work = self._work_tab.get(chat_id) if chat_id else None
            injected = work is not None and "tab_id" in TOOLS[name].schema["properties"] and op_args.get("tab_id") in (None, "")
            if injected:
                op_args["tab_id"] = work
            try:
                reply = await self.bridge.call(op, op_args, timeout=_timeout(name, args))
            except (NotConnected, BridgeTimeout) as exc:
                reply = {"ok": False, "code": "bridge", "error": str(exc)}
            result = (reply.get("result") or {}) if reply.get("ok") else {"error": reply.get("error") or "실패",
                                                                        "code": reply.get("code") or "error"}
            if injected and result.get("code") == "not_found" and "탭" in str(result.get("error")):
                result = {"error": "작업하던 탭이 닫힘 — list_tabs 로 탭을 고른 뒤 switch_tab", "code": "not_found"}
            if name == "look" and "error" not in result:
                result = await self._look(result, str(args["question"]))
            if chat_id and work is not None and "error" not in result:
                result = self._follow_tab(chat_id, name, args, result)
        # detail: 다시 실행할 때 ref 대신 쓸 요소 이름·프레임 등 (스크립트로 내보내기)
        detail = {k: result[k] for k in ("label", "frame_id", "page_changed", "method", "title", "work_tab")
                  if result.get(k) not in (None, "")}
        self.store.log(chat_id=chat_id, source=source, tool=name, args=args, ok="error" not in result,
                       summary=summarize(name, args, result), url=str(result.get("url") or ""), detail=detail)
        return result

    def _follow_tab(self, chat_id: str, name: str, args: dict, result: dict) -> dict:
        """작업 탭 옮기기: 에이전트가 switch_tab 했거나, 누른 결과 새 탭이 앞에 열렸을 때만 (사용자의 탭 이동은 따라가지 않는다)."""
        if name == "switch_tab":
            self._work_tab[chat_id] = int(args["tab_id"])
        elif name in ("click", "press_key", "type_text"):
            front = [t for t in result.get("new_tabs") or [] if t.get("active")]
            if front:
                self._work_tab[chat_id] = int(front[-1]["tab_id"])
                result = {**result, "work_tab": front[-1]["tab_id"]}
        elif name == "list_tabs":
            w = self._work_tab[chat_id]
            result = {**result, "tabs": [{**t, "work": t.get("tab_id") == w} for t in result.get("tabs", [])]}
        return result

    async def current_tab(self) -> Optional[int]:
        """탭을 알려 주지 않은 턴(import 등)은 시작할 때 사용자가 보던 탭에 묶는다."""
        try:
            reply = await self.bridge.call("active_tab", {}, timeout=5)
        except (NotConnected, BridgeTimeout):
            return None
        return (reply.get("result") or {}).get("tab_id") if reply.get("ok") else None

    async def _look(self, shot: dict, question: str) -> dict:
        """캡처 -> 비전 모델 답. 이미지는 돌려주지도 기록하지도 않는다 (답 글과 크기만)."""
        meta = {"url": shot.get("url", ""), "width": shot.get("width"), "height": shot.get("height"), "bytes": shot.get("bytes")}
        if self.vision is None:
            return {**meta, "error": "화면 보기(비전 모델)가 연결되지 않음", "code": "vision_off"}
        out = await asyncio.to_thread(self.vision, shot["image"], question)
        return {**meta, **out}

    def run_tool_sync(self, name: str, args: dict, chat_id: Optional[str], source: str) -> dict:
        """작업 스레드(로컬 LLM 턴)에서 부른다 -- 확장 연결은 서버 이벤트 루프에 있으므로 그쪽에서 실행."""
        if self.loop is None:
            return {"error": "서버가 아직 시작 중", "code": "bridge"}
        fut = asyncio.run_coroutine_threadsafe(self.run_tool(name, args, chat_id=chat_id, source=source), self.loop)
        return fut.result(timeout=640)

    # ---------------------------------------------------------------- 대화
    def status(self) -> dict:
        backends = {}
        for name, b in self.backends.items():
            ok, why = b.available()
            backends[name] = {"ok": ok, "why": why}
        running = self._running[0] if self._running else None
        return {"extension": self.bridge.status(), "backends": backends, "running": running,
                "work_tab": self._work_tab.get(running) if running else None}

    def create_chat(self, backend: str) -> dict:
        if backend not in self.backends:
            raise ServiceError(400, f"모르는 세션 {backend} — {', '.join(self.backends)}")
        return self.store.create_chat(backend)

    def chats(self) -> list[dict]:
        running = self._running[0] if self._running else None
        return [{**c, "running": c["id"] == running} for c in self.store.list_chats()]

    def chat(self, cid: str) -> dict:
        c = self.store.get_chat(cid)
        if not c:
            raise ServiceError(404, f"대화 {cid} 없음")
        return {"chat": c, "messages": self.store.messages(cid)}

    def start_turn(self, cid: str, text: str, tab_id: Optional[int] = None) -> tuple[threading.Event, Iterator[dict]]:
        """턴을 예약하고 이벤트 생성기를 돌려준다. 검증 실패(없음·빔·세션 꺼짐·다른 턴 실행 중)는 여기서 바로 예외."""
        chat = self.store.get_chat(cid)
        if not chat:
            raise ServiceError(404, f"대화 {cid} 없음")
        text = (text or "").strip()
        if not text:
            raise ServiceError(400, "할 일이 비었음")
        backend = self.backends.get(chat["backend"])
        if backend is None:
            raise ServiceError(400, f"이 대화의 세션({chat['backend']})을 쓸 수 없음")
        ok, why = backend.available()
        if not ok:
            raise ServiceError(503, why)
        with self._lock:
            if self._running:
                raise ServiceError(409, "다른 대화가 브라우저를 쓰는 중 — 끝나거나 중지한 뒤 다시")
            token = threading.Event()
            self._running = (cid, token)
            if tab_id is not None:
                self._work_tab[cid] = int(tab_id)
        history = self.store.messages(cid)
        self.store.add_message(cid, "user", text)
        self.store.touch(cid, title=text[:40])
        return token, self._turn(backend, Turn(chat=chat, history=history, text=text, cancel=token))

    def _turn(self, backend: ChatBackend, turn: Turn) -> Iterator[dict]:
        cid = turn.chat["id"]
        steps: list[dict] = []
        text, final, error = "", "", ""
        try:
            for ev in backend.run(turn):
                t = ev.get("type")
                if t == "session":
                    self.store.mark_claude_started(cid)
                    continue
                if t == "tool_result":
                    steps.append(ev)
                elif t == "text":
                    text += ev.get("text", "")
                elif t == "done":
                    final = ev.get("text") or text
                elif t == "error":
                    error = ev.get("message", "")
                yield ev
        finally:
            if turn.cancel.is_set() and not error:
                error = "사용자가 중지함"
            self.store.add_message(cid, "assistant", final or ("" if error else text), steps=steps, error=error)
            self.store.touch(cid)
            with self._lock:
                if self._running and self._running[1] is turn.cancel:
                    self._running = None
                    self._work_tab.pop(cid, None)

    def stop(self, cid: str, token: Optional[threading.Event] = None) -> bool:
        with self._lock:
            if self._running and self._running[0] == cid and (token is None or self._running[1] is token):
                self._running[1].set()
                return True
        return False

    def _cancelled(self, cid: str) -> bool:
        r = self._running
        return bool(r and r[0] == cid and r[1].is_set())
