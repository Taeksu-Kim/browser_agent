"""진짜 Whisper 점검: 음성 서버(8013)의 한국어 TTS 로 만든 말을 Chromium 가짜 마이크에 넣고, 패널 🎤(Whisper)를 눌러
말 끝 감지 → /api/stt → 음성 서버 Whisper → 입력창 글까지 실제 경로로 확인한다.

    python tools/stt_server.py --tts &       # 받아적기 + 점검용 한국어 TTS (supertonic 필요)
    python tools/voice_check.py

모델(LLM)은 쓰지 않는다 (마이크 모드 '입력창에만'). 음성 서버가 꺼져 있으면 바로 끝낸다.
"""
from __future__ import annotations

import io
import json
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from sonnun.app import create_app  # noqa: E402
from sonnun.stt import make_stt  # noqa: E402

SENTENCES = ["이 페이지 요약해줘", "검색창에 부산 날씨 입력하고 검색해줘", "S18 결제하기 버튼 눌러줘"]
VOICE = "http://127.0.0.1:8013"
TMP = Path(tempfile.mkdtemp(prefix="sonnun_voice_"))
EXT = str(ROOT / "extension")


def tts_wav(text: str, path: Path) -> float:
    req = urllib.request.Request(VOICE + "/tts", data=json.dumps({"text": text, "voice": "F1"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with wave.open(io.BytesIO(data)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    n = int(len(x) * 16000 / sr)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)          # 16kHz 로
    rng = np.random.default_rng(0)
    pad = lambda s: rng.normal(0, 0.002, int(16000 * s))                         # noqa: E731 -- 방 소음 비슷한 아주 작은 잡음
    y = np.concatenate([pad(1.0), y, pad(3.5)])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return len(x) / sr


def norm(s: str) -> str:
    return re.sub(r"[\s.,!?~]", "", s)


def main() -> int:
    try:
        urllib.request.urlopen(VOICE + "/health", timeout=2)
    except Exception:  # noqa: BLE001
        print("음성 서버(8013)가 꺼져 있음 — 위 docstring 의 명령으로 켠 뒤 다시")
        return 2
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = f"http://127.0.0.1:{port}"
    app = create_app(TMP / "v.db", llm_factory=lambda: None, llm_ping=lambda: (True, ""),
                     server_url=server, vision=lambda *a: {}, stt=make_stt(VOICE))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(1.5)
    fallback = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    rows = []
    with sync_playwright() as p:
        for i, sent in enumerate(SENTENCES):
            wav = TMP / f"s{i}.wav"
            speech_s = tts_wav(sent, wav)
            kw = dict(headless=True, args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                                           "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                           f"--use-file-for-fake-audio-capture={wav}"])
            try:
                ctx = p.chromium.launch_persistent_context(str(TMP / f"profile{i}"), channel="chromium", **kw)
            except Exception:  # noqa: BLE001
                ctx = p.chromium.launch_persistent_context(str(TMP / f"profile{i}"), executable_path=str(fallback[-1]), **kw)
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
            ext_id = sw.url.split("/")[2]
            sw.evaluate("u => chrome.storage.local.set({server: u, voiceEngine: 'whisper', voiceMode: 'fill'})", server)
            panel = ctx.new_page()
            panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
            for _ in range(50):                         # 상태(음성 서버 켜짐)를 받을 때까지
                if panel.evaluate("!!(status && status.stt && status.stt.ok)"):
                    break
                time.sleep(0.2)
            t0 = time.time()
            panel.click("#mic")
            got = ""
            for _ in range(150):
                got = panel.input_value("#input")
                bar = panel.inner_text("#voiceBar") if panel.is_visible("#voiceBar") else ""
                if got or "못" in bar or "않" in bar:
                    break
                time.sleep(0.2)
            rows.append({"말한 문장": sent, "받아적음": got, "같음": norm(got) == norm(sent), "말 길이(초)": round(speech_s, 1),
                         "누름→글(초)": round(time.time() - t0, 1), "막대": bar})
            print(json.dumps(rows[-1], ensure_ascii=False))
            ctx.close()
    srv.should_exit = True
    ok = sum(r["같음"] for r in rows)
    print(f"\n{ok}/{len(rows)} 글자까지 같음 (공백·문장부호 무시)")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
