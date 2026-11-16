"""서버 -- 확장 연결(WebSocket) + 제품 API(사이드 패널이 씀) + 도구 API(MCP 다리·import 가 씀). 127.0.0.1 고정.

    python -m sonnun serve            # http://127.0.0.1:8771
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sonnun.bridge import ExtensionBridge
from sonnun.chat import ClaudeBackend, LocalBackend
from sonnun.service import ServiceError, SonnunService
from sonnun.store import Store
from sonnun.stt import Stt, make_stt
from sonnun.tools import TOOLS
from sonnun.vision import Vision, make_vision

DEFAULT_PORT = 8771
PLAYGROUND = Path(__file__).resolve().parent / "playground"


class NewChat(BaseModel):
    backend: str = "local"


class SendIn(BaseModel):
    text: str
    tab_id: Optional[int] = None          # 작업 탭 (패널이 보고 있던 탭). 없으면 지금 사용자가 보는 탭


def create_app(db_path: Path, *, llm_factory: Optional[Callable[[], Any]] = None,
               llm_ping: Optional[Callable[[], tuple[bool, str]]] = None,
               claude: Optional[ClaudeBackend] = None, server_url: str = f"http://127.0.0.1:{DEFAULT_PORT}",
               vision: Optional[Vision] = None, stt: Optional[Stt] = None) -> FastAPI:
    """``llm_factory``·``llm_ping``: 로컬 모델 (테스트는 가짜 주입). ``claude``: Claude Code 세션 (테스트는 가짜 runner)."""
    bridge = ExtensionBridge()
    svc = SonnunService(Store(db_path), bridge)
    if llm_factory is None or llm_ping is None:
        from sonnun.llm import default_llm
        f, p = default_llm()
        llm_factory, llm_ping = llm_factory or f, llm_ping or p
    if vision is None:
        from sonnun.llm import MODEL, base_url
        vision = make_vision(base_url(), MODEL)
    svc.vision = vision
    stt = stt or make_stt()          # 음성 받아적기 (Whisper 음성 서버). Google 음성 인식은 패널이 직접 쓴다
    svc.backends = {
        "local": LocalBackend(llm_factory, llm_ping, svc.run_tool_sync),
        "claude": claude or ClaudeBackend(server_url),
    }

    app = FastAPI(title="sonnun")
    app.state.svc = svc
    # 사이드 패널(chrome-extension://)은 host_permissions 로 CORS 없이 부르지만, 다른 확장·도구를 위해 열어 둔다 (127.0.0.1 만 듣는다)
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^chrome-extension://.*$", allow_methods=["*"], allow_headers=["*"])

    @app.on_event("startup")
    async def _startup() -> None:
        svc.loop = asyncio.get_running_loop()

    @app.exception_handler(ServiceError)
    async def _svc_error(_req: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    # ---------------------------------------------------------------- 확장 연결
    @app.websocket("/ws/extension")
    async def ws_extension(ws: WebSocket) -> None:
        await ws.accept()
        await bridge.attach(ws)
        try:
            while True:
                bridge.on_message(await ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            bridge.detach(ws)

    # ---------------------------------------------------------------- 상태·도구
    @app.get("/api/status")
    def status() -> dict:
        ok, why = stt.available()
        return {**svc.status(), "stt": {"ok": ok, "why": why}}

    # ---------------------------------------------------------------- 음성 받아적기 (패널 🎤, 엔진 Whisper)
    @app.post("/api/stt")
    async def speech_to_text(request: Request) -> dict:
        """본문 = 내 말 녹음 (PCM16 little-endian 16kHz mono). 녹음·글은 저장하지 않는다 (대화에 보낸 글만 남음)."""
        from sonnun.stt import MAX_BYTES
        body = await request.body()
        if not body or len(body) % 2:
            raise ServiceError(400, "녹음이 비었거나 형식이 맞지 않음 (PCM16 16kHz)")
        if len(body) > MAX_BYTES:
            raise ServiceError(413, "녹음이 너무 김 (1분까지)")
        out = await asyncio.to_thread(stt, body)
        if "error" in out:
            raise ServiceError(503 if out.get("code") == "stt_down" else 502, out["error"])
        return out

    @app.get("/api/tools")
    def tools() -> list[dict]:
        return [{"name": t.name, "description": t.description, "schema": t.schema, "level": t.level} for t in TOOLS.values()]

    @app.post("/api/tools/{name}")
    async def call_tool(name: str, request: Request, x_sonnun_chat: Optional[str] = Header(default=None),
                        x_sonnun_source: Optional[str] = Header(default=None)) -> dict:
        try:
            args = await request.json()
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            raise ServiceError(400, "인자는 JSON 객체")
        source = x_sonnun_source if x_sonnun_source in ("claude", "local", "api") else "api"
        return await svc.run_tool(name, args, chat_id=x_sonnun_chat or None, source=source)

    @app.get("/api/activity")
    def activity(limit: int = 100, chat: Optional[str] = None) -> list[dict]:
        return svc.store.activity(min(max(limit, 1), 500), chat)

    # ---------------------------------------------------------------- 대화
    @app.get("/api/chats")
    def chats() -> list[dict]:
        return svc.chats()

    @app.post("/api/chats")
    def new_chat(body: NewChat) -> dict:
        return svc.create_chat(body.backend)

    @app.get("/api/chats/{cid}")
    def get_chat(cid: str) -> dict:
        return svc.chat(cid)

    @app.get("/api/chats/{cid}/script")
    def script(cid: str, format: str = "sonnun") -> PlainTextResponse:
        """이 대화에서 성공한 조작을 다시 실행하는 Python 스크립트 (format: sonnun | playwright)."""
        from sonnun.export import to_script
        if format not in ("sonnun", "playwright"):
            raise ServiceError(400, "format 은 sonnun 또는 playwright")
        chat = svc.chat(cid)["chat"]
        acts = list(reversed(svc.store.activity(1000, cid)))
        if not acts:
            raise ServiceError(404, "이 대화에는 브라우저 조작 기록이 없음")
        return PlainTextResponse(to_script(acts, format, title=chat["title"]))  # type: ignore[arg-type]

    @app.post("/api/chats/{cid}/stop")
    def stop(cid: str) -> dict:
        return {"stopped": svc.stop(cid)}

    @app.post("/api/chats/{cid}/messages")
    async def send(cid: str, body: SendIn) -> StreamingResponse:
        tab_id = body.tab_id if body.tab_id is not None else await svc.current_tab()
        token, gen = svc.start_turn(cid, body.text, tab_id)   # 검증 실패는 여기서 4xx
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def worker() -> None:
            # 턴은 전용 스레드가 돌린다 -- 화면이 연결을 끊으면 아래 finally 가 중지를 걸고, 이 스레드가 생성기를 닫는다
            try:
                for ev in gen:
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "kind": "internal", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                gen.close()
                loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=worker, daemon=True, name=f"sonnun-turn-{cid}").start()

        async def stream():
            try:
                while True:
                    ev = await q.get()
                    if ev is None:
                        break
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            finally:
                svc.stop(cid, token)                           # 끝까지 읽었으면 아무 일도 안 함

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # 플레이그라운드: 시나리오별로 눌리는지 눈으로 보는 테스트 페이지 (python -m sonnun demo)
    app.mount("/playground", StaticFiles(directory=PLAYGROUND, html=True), name="playground")
    return app
