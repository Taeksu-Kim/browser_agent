"""음성 받아적기 (Whisper) — 패널이 녹음한 내 말(PCM16 16kHz mono)을 음성 서버로 넘긴다.

음성 서버는 ``tools/stt_server.py`` (faster-whisper large-v3-turbo, GPU 약 1.5GB, 포트 8013). 같은 약속
(``POST /stt`` 본문 = PCM16 16kHz mono → ``{text}``, ``GET /health``)을 지키는 다른 서버면 ``SONNUN_STT_URL`` 로.
꺼져 있어도 손눈서브는 뜨고, 패널에서 Whisper 를 고른 🎤 만 "켜는 법"을 보여 준다 (Google 음성 인식은 이것과 무관).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

DEFAULT_URL = "http://127.0.0.1:8013"
START_HINT = "음성 서버가 꺼져 있음 — 켜기: python tools/stt_server.py (faster-whisper 필요)"
MAX_BYTES = 16000 * 2 * 60          # 1분 (패널은 30초에서 끊는다)


class Stt(Protocol):
    def available(self) -> tuple[bool, str]: ...
    def __call__(self, pcm: bytes) -> dict: ...


class HttpStt:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self._cache: tuple[float, tuple[bool, str]] = (0.0, (False, ""))

    def available(self) -> tuple[bool, str]:
        """패널이 5초마다 상태를 물으니 10초 캐시 (음성 서버를 두드리지 않게)."""
        at, val = self._cache
        if time.time() - at < 10:
            return val
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=1) as r:
                val = (True, str(json.load(r).get("stt", "whisper")))
        except Exception:  # noqa: BLE001
            val = (False, START_HINT)
        self._cache = (time.time(), val)
        return val

    def __call__(self, pcm: bytes) -> dict:
        req = urllib.request.Request(self.base + "/stt", data=pcm, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
        except urllib.error.HTTPError as exc:
            return {"error": f"음성 서버 오류 {exc.code}: {exc.read().decode(errors='replace')[:200]}", "code": "stt_error"}
        except Exception:  # noqa: BLE001
            self._cache = (0.0, (False, START_HINT))
            return {"error": START_HINT, "code": "stt_down"}
        self._cache = (time.time(), (True, "whisper"))
        return {"text": str(out.get("text", "")).strip(), "ms": out.get("ms"), "audio_s": out.get("audio_s")}


def make_stt(base_url: str | None = None) -> Stt:
    return HttpStt(base_url or os.environ.get("SONNUN_STT_URL") or DEFAULT_URL)


class FuncStt:
    """테스트용: 함수 하나로 (가짜 받아적기)."""
    def __init__(self, fn: Callable[[bytes], dict], ok: bool = True) -> None:
        self.fn, self.ok = fn, ok

    def available(self) -> tuple[bool, str]:
        return (True, "가짜") if self.ok else (False, START_HINT)

    def __call__(self, pcm: bytes) -> dict:
        return self.fn(pcm)
