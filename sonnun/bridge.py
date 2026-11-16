"""확장과의 연결 -- 확장(background.js)이 서버의 /ws/extension 에 WebSocket 으로 붙고, 서버가 명령을 보낸다.

명령 {"id", "op", "args"} -> 답 {"id", "ok": true, "result"} | {"id", "ok": false, "code", "error"}.
확장은 한 번에 하나만 붙는다 (새로 붙으면 이전 연결을 닫고, 기다리던 명령은 실패로 끝낸다).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from typing import Any, Optional


class NotConnected(RuntimeError):
    pass


class BridgeTimeout(RuntimeError):
    pass


NOT_CONNECTED = ("Chrome 의 손눈서브 확장이 서버에 붙어 있지 않음 — Chrome 을 열고 확장이 켜져 있는지, "
                 "패널 설정의 서버 주소가 맞는지 확인")


class ExtensionBridge:
    def __init__(self) -> None:
        self.ws: Any = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.pending: dict[str, asyncio.Future] = {}
        self.info: dict = {}
        self.since: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def status(self) -> dict:
        return {"connected": self.connected, "since": self.since, "version": self.info.get("version"),
                "trusted": self.info.get("trusted", False), "paused": self.info.get("paused", False)}

    async def attach(self, ws: Any) -> None:
        old, self.ws = self.ws, ws
        self.loop = asyncio.get_running_loop()
        self.since = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.info = {}
        self._fail_pending("확장 연결이 새로 바뀜")
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001 -- 이미 끊긴 연결
                pass

    def detach(self, ws: Any) -> None:
        if self.ws is ws:
            self.ws = None
            self._fail_pending("확장 연결이 끊김")

    def on_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "hello":
            self.info = {"version": msg.get("version"), "ua": msg.get("ua")}
            return
        if msg.get("type") == "settings":              # 확장 설정 중 동작에 영향 주는 것 (진짜 입력·멈춤)
            self.info.update(trusted=bool(msg.get("trusted")), paused=bool(msg.get("paused")))
            return
        fut = self.pending.get(str(msg.get("id")))
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def _fail_pending(self, why: str) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(NotConnected(why))
        self.pending.clear()

    async def call(self, op: str, args: dict, timeout: float = 40.0) -> dict:
        """확장에 명령 하나. 확장의 답(ok/result/error)을 그대로 돌려준다."""
        ws = self.ws
        if ws is None or self.loop is None:
            raise NotConnected(NOT_CONNECTED)
        rid = uuid.uuid4().hex
        fut = self.loop.create_future()
        self.pending[rid] = fut
        try:
            await ws.send_text(json.dumps({"id": rid, "op": op, "args": args}, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise BridgeTimeout(f"확장이 {timeout:.0f}초 안에 답하지 않음 ({op})") from None
        finally:
            self.pending.pop(rid, None)
