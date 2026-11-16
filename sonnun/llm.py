"""로컬 LLM 연결 — OpenAI 호환 서버(vLLM 등). 기본: Qwen3.8-27B-INT4 on :8002. 요청마다 thinking 을 끈다.

``SONNUN_LLM_URL`` (예: http://127.0.0.1:8002/v1) · ``SONNUN_LLM_MODEL`` 로 바꾼다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Any, Callable

from sonnun.chat import BASIC_AGENT, LOCAL_START

MODEL = os.environ.get("SONNUN_LLM_MODEL", "qwen3.8-27b-redhat-int4")


def base_url() -> str:
    if os.environ.get("SONNUN_LLM_URL"):
        return os.environ["SONNUN_LLM_URL"].rstrip("/")
    try:   # WSL 에서는 localhost 가 안 될 수 있어 이 컴퓨터의 IP 를 쓴다
        ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout.split()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        ip = "127.0.0.1"
    return f"http://{ip}:8002/v1"


def default_llm() -> tuple[Callable[[], Any], Callable[[], tuple[bool, str]]]:
    url = base_url()

    def factory() -> Any:
        if str(BASIC_AGENT) not in sys.path:
            sys.path.insert(0, str(BASIC_AGENT))
        from agent.core.llm import OpenAICompatLLM
        return OpenAICompatLLM(base_url=url, model=MODEL, timeout=180, max_retries=1,
                               sampling={"temperature": 0, "chat_template_kwargs": {"enable_thinking": False}})

    def ping() -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{url}/models", timeout=3) as r:
                models = [m.get("id") for m in json.load(r).get("data", [])]
        except (OSError, ValueError) as exc:
            return False, f"로컬 LLM 이 꺼져 있습니다 ({url}: {type(exc).__name__}). {LOCAL_START}"
        if MODEL not in models:
            return False, f"vLLM 에 {MODEL} 이 없습니다 (있는 것: {', '.join(map(str, models)) or '없음'})"
        return True, ""

    return factory, ping
