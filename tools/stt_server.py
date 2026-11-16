"""음성 받아적기 서버 (Whisper) — 패널 🎤 의 Whisper 엔진이 쓴다. 손눈서브 서버와 따로 띄운다 (GPU·패키지가 달라서).

    pip install faster-whisper fastapi uvicorn numpy
    python tools/stt_server.py [--port 8013] [--model large-v3-turbo] [--device cuda] [--tts]

    GET  /health                → {ok, stt}
    POST /stt                   본문 = PCM16 little-endian 16kHz mono → {text, ms, audio_s}
    POST /tts  (--tts 일 때만)   {text, voice} → audio/wav  — tools/voice_check.py 가 점검용 말소리를 만들 때 (supertonic 필요)

large-v3-turbo int8 은 GPU 약 1.5GB (2026-11-16 실측 +1,458 MiB, 같은 모델을 쓰는 다른 음성 서버로 잼).
말 구간은 패널이 잘라 보내므로 여기서는 VAD 를 다시 하지 않는다. 127.0.0.1 만 듣는다.
"""
from __future__ import annotations

import argparse
import io
import threading
import time

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel


class TTSIn(BaseModel):
    text: str
    voice: str = "F1"


def create_app(model: str, device: str, compute_type: str, tts: bool) -> FastAPI:
    try:
        import torch  # noqa: F401 — torch 와 같이 깔린 libcublas.so.12 를 ctranslate2(faster-whisper)가 찾게
    except ImportError:
        pass          # torch 가 없으면 시스템 CUDA 라이브러리를 쓴다
    from faster_whisper import WhisperModel

    stt = WhisperModel(model, device=device, compute_type=compute_type)
    lock = threading.Lock()

    def transcribe(pcm: np.ndarray) -> str:
        segs, _ = stt.transcribe(pcm, language="ko", beam_size=1, condition_on_previous_text=False, vad_filter=False)
        return "".join(s.text for s in segs).strip()

    transcribe(np.zeros(16000, dtype=np.float32))          # 첫 요청이 느리지 않게 한 번 미리
    synth = None
    if tts:
        from supertonic import TTS
        engine = TTS(model="supertonic-3")
        synth = (engine, {v: engine.get_voice_style(v) for v in ("F1", "M1")})

    app = FastAPI(title="sonnun-stt")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "stt": f"whisper {model} {compute_type} {device}"}

    @app.post("/stt")
    async def post_stt(request: Request) -> dict:
        body = await request.body()
        if not body or len(body) % 2:
            raise HTTPException(422, "PCM16 바이트가 아닙니다")
        pcm = np.frombuffer(body, dtype="<i2").astype(np.float32) / 32768.0
        t = time.perf_counter()
        with lock:
            text = transcribe(pcm)
        return {"text": text, "ms": round((time.perf_counter() - t) * 1000), "audio_s": round(len(pcm) / 16000, 2)}

    @app.post("/tts")
    def post_tts(req: TTSIn) -> Response:
        if synth is None:
            raise HTTPException(404, "--tts 로 켜야 합니다")
        import soundfile as sf
        engine, styles = synth
        wav, _ = engine.synthesize(req.text, voice_style=styles.get(req.voice, styles["F1"]), lang="ko")
        buf = io.BytesIO()
        sf.write(buf, np.asarray(wav, dtype=np.float32).squeeze(), engine.sample_rate, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav")

    return app


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="cuda", help="cuda 또는 cpu")
    ap.add_argument("--compute-type", default=None, help="기본: cuda=int8_float16, cpu=int8")
    ap.add_argument("--tts", action="store_true", help="점검용 /tts 도 켬 (supertonic)")
    a = ap.parse_args()
    ct = a.compute_type or ("int8_float16" if a.device == "cuda" else "int8")
    uvicorn.run(create_app(a.model, a.device, ct, a.tts), host="127.0.0.1", port=a.port)


if __name__ == "__main__":
    main()
