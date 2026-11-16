"""내보낸 스크립트가 쓰는 도우미 -- ref(그때그때 바뀌는 번호) 대신 요소 이름으로 찾아 누르고 입력한다.

    from sonnun.client import Sonnun
    from sonnun.replay import click_label, type_label
    b = Sonnun()
    type_label(b, "검색어", "부산 날씨", submit=True)

이름이 정확히 같은 요소를 먼저, 없으면 그 이름으로 시작하는 요소를 고른다. 화면 밖·프레임·shadow DOM 도 찾는다 (find 도구).
결과가 늦게 뜨는 페이지를 위해 잠깐 기다리며 다시 찾는다.
"""
from __future__ import annotations

import time
from typing import Optional

from sonnun.client import Sonnun, SonnunError


def element(b: Sonnun, label: str, *, wait_sec: float = 5.0) -> dict:
    end = time.time() + wait_sec
    while True:
        els = b.find(text=label)["elements"]
        hit = next((e for e in els if e.get("label") == label), None) or \
            next((e for e in els if e.get("label", "").startswith(label)), None)
        if hit:
            return hit
        if time.time() > end:
            raise SonnunError("not_found", f'"{label}" 요소를 찾지 못함 — 페이지가 바뀌었을 수 있음')
        time.sleep(0.5)


def click_label(b: Sonnun, label: str, *, accept_dialog: Optional[bool] = None) -> dict:
    return b.click(ref=element(b, label)["ref"], accept_dialog=accept_dialog)


def type_label(b: Sonnun, label: str, text: str, *, submit: bool = False) -> dict:
    return b.type_text(ref=element(b, label)["ref"], text=text, submit=submit)


def hover_label(b: Sonnun, label: str) -> dict:
    return b.hover(ref=element(b, label)["ref"])
