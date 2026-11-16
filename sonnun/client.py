"""import 창구 -- 다른 파이썬 코드가 떠 있는 손눈서브 서버를 통해 내 브라우저를 보고 조작한다.

    from sonnun.client import Sonnun
    b = Sonnun()                      # http://127.0.0.1:8771
    page = b.get_page()               # {"title", "url", "elements": [{"ref", "kind", "label"}], "text", ...}
    b.type_text(ref="5", text="날씨", submit=True)

모든 호출은 활동 기록에 남고(출처 import), 사이트 권한·멈춤도 똑같이 적용된다. 실패는 ``SonnunError``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


class SonnunError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Sonnun:
    def __init__(self, url: str = "http://127.0.0.1:8771", timeout: float = 660) -> None:   # ask_user 는 최대 10분 기다린다
        self.url, self.timeout = url.rstrip("/"), timeout

    def status(self) -> dict:
        """서버·확장 상태 (확장 연결, 진짜 입력·멈춤 설정, 세션 사용 가능 여부)."""
        try:
            with urllib.request.urlopen(f"{self.url}/api/status", timeout=10) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, OSError) as exc:
            raise SonnunError("server_down", f"손눈서브 서버에 연결할 수 없음 ({self.url}): {exc}") from None

    def call(self, name: str, **args: Any) -> dict:
        args = {k: v for k, v in args.items() if v is not None}
        req = urllib.request.Request(f"{self.url}/api/tools/{name}", data=json.dumps(args, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", "X-Sonnun-Source": "api"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                result = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            raise SonnunError("http", f"{exc.code}: {exc.read().decode(errors='replace')[:300]}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise SonnunError("server_down", f"손눈서브 서버에 연결할 수 없음 ({self.url}) — python -m sonnun serve: {exc}") from None
        if "error" in result:
            raise SonnunError(result.get("code") or "error", result["error"])
        return result

    # 눈
    def list_tabs(self) -> dict:
        return self.call("list_tabs")

    def get_page(self, tab_id: Optional[int] = None, max_elements: Optional[int] = None) -> dict:
        return self.call("get_page", tab_id=tab_id, max_elements=max_elements)

    def read_text(self, start: int = 0, length: Optional[int] = None, tab_id: Optional[int] = None,
                  frame_id: Optional[int] = None) -> dict:
        return self.call("read_text", start=start, length=length, tab_id=tab_id, frame_id=frame_id)

    def get_selection(self, tab_id: Optional[int] = None) -> dict:
        return self.call("get_selection", tab_id=tab_id)

    def find(self, text: str, tab_id: Optional[int] = None) -> dict:
        """이름으로 요소 찾기 (화면 밖·프레임·shadow DOM 포함)."""
        return self.call("find", text=text, tab_id=tab_id)

    def look(self, question: str, tab_id: Optional[int] = None) -> dict:
        """화면을 그림으로 보고 로컬 비전 모델의 답 ({"answer", ...})."""
        return self.call("look", question=question, tab_id=tab_id)

    # 손
    def navigate(self, url: str, tab_id: Optional[int] = None) -> dict:
        return self.call("navigate", url=url, tab_id=tab_id)

    def click(self, ref: str, tab_id: Optional[int] = None, accept_dialog: Optional[bool] = None,
              dialog_text: Optional[str] = None) -> dict:
        return self.call("click", ref=str(ref), tab_id=tab_id, accept_dialog=accept_dialog, dialog_text=dialog_text)

    def type_text(self, ref: str, text: str, submit: bool = False, tab_id: Optional[int] = None) -> dict:
        return self.call("type_text", ref=str(ref), text=text, submit=submit, tab_id=tab_id)

    def wait_for(self, text: str, gone: bool = False, timeout_sec: Optional[int] = None, tab_id: Optional[int] = None) -> dict:
        return self.call("wait_for", text=text, gone=gone, timeout_sec=timeout_sec, tab_id=tab_id)

    def press_key(self, key: str, ref: Optional[str] = None, tab_id: Optional[int] = None) -> dict:
        return self.call("press_key", key=key, ref=ref, tab_id=tab_id)

    def hover(self, ref: str, tab_id: Optional[int] = None) -> dict:
        return self.call("hover", ref=str(ref), tab_id=tab_id)

    def ask_user(self, message: str, timeout_sec: Optional[int] = None) -> dict:
        return self.call("ask_user", message=message, timeout_sec=timeout_sec)

    def scroll(self, direction: str = "down", tab_id: Optional[int] = None) -> dict:
        return self.call("scroll", direction=direction, tab_id=tab_id)

    def go_back(self, tab_id: Optional[int] = None) -> dict:
        return self.call("go_back", tab_id=tab_id)

    def switch_tab(self, tab_id: int) -> dict:
        return self.call("switch_tab", tab_id=tab_id)
