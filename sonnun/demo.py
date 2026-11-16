"""플레이그라운드 시연 -- 내 Chrome 에서 플레이그라운드(/playground/) 시나리오를 하나씩 천천히 실행하고, 페이지 점수판을 읽어 표로 찍는다.

    python -m sonnun demo [--slow 1.2]

모델 없이 코드가 직접 손눈서브 도구를 부른다 (도구 자체가 어디까지 되는지 보는 용도).
판정은 페이지 점수판(이벤트를 실제로 받았는지)으로 한다 -- 도구가 "성공"이라고 답한 것으로 판정하지 않는다.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from sonnun.client import Sonnun, SonnunError


@dataclass(frozen=True)
class Step:
    sid: str
    action: str                 # click | type | click_wait | hover_click | click_key | click_newtab | approve_click
    label: str                  # get_page 요소 이름
    text: str = ""              # type: 넣을 글 / click_wait·hover_click: 다음에 누를 요소 이름 / click_key: 키
    supported: bool = True      # 지금 지원하는 것으로 기대하나


STEPS = [
    Step("S1", "click", "S1 누르기"),
    Step("S2", "type", "S2 입력 칸", "손눈"),
    Step("S3", "click", "S3 동의"),
    Step("S4", "type", "S4 도시", "부산"),
    Step("S5", "type", "S5 메모", "메모 완료"),
    Step("S6", "click", "S6 멀리 있는 버튼"),
    Step("S7", "click", "S7 iframe 버튼"),
    Step("S8", "click", "S8 다른 출처 버튼"),
    Step("S9", "click", "S9 shadow 버튼"),
    Step("S10", "click", "S10 닫힌 shadow 버튼", supported=False),     # 진짜 입력(CDP)을 켜면 됨
    Step("S11", "click", "S11 진짜 클릭만", supported=False),
    Step("S12", "type", "S12 iframe 입력", "프레임 입력"),
    Step("S13", "type", "S13 수량", "324"),
    Step("S14", "type", "S14 금액", "1234"),
    Step("S15", "type", "S15 송금액", "324"),
    Step("S16", "type", "S16 날짜", "2026-11-16"),
    Step("S18", "approve_click", "S18 결제하기"),                 # 사용자가 패널에서 허용해야 함 (--approve 일 때만)
    Step("S19", "click", "S19 기록 지우기"),                      # 확인창 -> 기본 '취소'
    Step("S20", "click", "S20 알림 띄우기"),                      # 알림창 -> 막히지 않고 기록
    Step("S21", "click_newtab", "S21 새 탭 열기"),
    Step("S22", "click_wait", "S22 불러오기", "S22 확인"),
    Step("S23", "hover_click", "S23 메뉴", "S23 하위 항목"),
    Step("S24", "click_key", "S24 창 열기", "Escape"),
]


def board(b: Sonnun) -> dict[str, str]:
    """페이지 점수판 -> {S1: "성공"|"대기"}"""
    # 이름과 상태가 줄을 달리해 나올 수 있어(레이아웃에 따라) 줄이 아니라 차례로 짝짓는다: S번호 다음에 처음 나오는 상태
    text = b.read_text(start=0, length=4000)["text"]
    out: dict[str, str] = {}
    sid = None
    for m in re.finditer(r"\b(S\d+)\b|(성공|대기)", text):
        if m.group(1):
            sid = m.group(1)
        elif sid and sid not in out:
            out[sid] = m.group(2)
            sid = None
    return out


def find(b: Sonnun, label: str, max_scrolls: int = 15) -> Optional[dict]:
    """이름으로 요소 찾기. 화면 근처에 없으면 페이지 끝에 닿을 때까지 아래로 스크롤하며 다시 본다
    (get_page 는 화면 근처 요소만 준다)."""
    last_y = None
    for _ in range(max_scrolls + 1):
        for e in b.get_page(max_elements=80)["elements"]:
            if e.get("label", "").startswith(label):
                return e
        y = b.scroll(direction="down")["y"]
        if y == last_y:           # 더 내려가지 않음 = 끝
            return None
        last_y = y
    return None


def run(b: Sonnun, server: str, *, slow: float = 1.0, out: Callable[[str], None] = print, approve: bool = False) -> list[dict]:
    trusted = bool(b.status()["extension"].get("trusted"))
    out(f"진짜 입력(CDP): {'켜짐' if trusted else '꺼짐 — 패널 설정에서 켜면 S10·S11 도 기대함'}")
    b.navigate(url=f"{server.rstrip('/')}/playground/")
    time.sleep(max(slow, 0.5))          # iframe 이 뜰 시간
    home = b.get_page()["tab_id"]
    rows = []
    for st in STEPS:
        note = ""
        if st.action == "approve_click" and not approve:
            rows.append({"sid": st.sid, "이름": st.label, "기대": "승인 필요", "결과": "건너뜀", "예상대로": True,
                         "메모": "패널 승인이 필요해 건너뜀 (--approve 로 실행하면 패널에 승인 카드가 뜸)"})
            continue
        el = find(b, st.label)
        if el is None:
            note = "요소를 찾지 못함"
        else:
            where = f" (프레임 {el['frame_id']})" if el.get("frame_id") else (" (shadow DOM)" if el.get("shadow") else "")
            verb = {"type": "입력: " + st.text, "hover_click": f"마우스 올리기 → {st.text}", "click_key": f"누르기 → {st.text} 키",
                    "click_wait": f"누르기 → '{st.text}' 기다리기 → 누르기",
                    "approve_click": "누르기 (패널에서 '허용'을 눌러 주세요)"}.get(st.action, "누르기")
            out(f"  {st.sid} {st.label}{where} → {verb}")
            try:
                if st.action == "type":
                    b.type_text(ref=el["ref"], text=st.text)
                elif st.action == "hover_click":
                    b.hover(ref=el["ref"])
                    nxt = find(b, st.text)
                    if nxt is None:
                        raise SonnunError("not_found", f"마우스를 올린 뒤에도 '{st.text}' 가 안 보임")
                    b.click(ref=nxt["ref"])
                elif st.action == "click_wait":
                    b.click(ref=el["ref"])
                    if not b.wait_for(text=st.text, timeout_sec=10)["met"]:
                        raise SonnunError("timeout", f"'{st.text}' 가 10초 안에 안 나타남")
                    b.click(ref=find(b, st.text)["ref"])
                elif st.action == "click_key":
                    b.click(ref=el["ref"])
                    b.press_key(key=st.text)
                else:
                    r = b.click(ref=el["ref"])
                    if r.get("dialogs"):
                        note = "; ".join(f"{d['type']} '{d['message']}' → {d['answer']}" for d in r["dialogs"])
                    if st.action == "click_newtab":
                        note = f"새 탭 {len(r.get('new_tabs') or [])}개 → 플레이그라운드로 돌아감"
                        time.sleep(slow)
                        b.switch_tab(tab_id=home)
            except SonnunError as exc:
                note = f"오류 {exc.code}: {exc}"
        time.sleep(slow)
        state = board(b).get(st.sid, "?")
        # 다음 시나리오를 위해 맨 위로 (스크롤이 멈출 때까지)
        y = None
        while True:
            ny = b.scroll(direction="up")["y"]
            if ny == y or ny == 0:
                break
            y = ny
        ok = state == "성공"
        expect = st.supported or (trusted and st.sid in ("S10", "S11"))
        rows.append({"sid": st.sid, "이름": st.label, "기대": "됨" if expect else "안 됨", "결과": state,
                     "예상대로": ok == expect, "메모": note})
    return rows


def print_rows(rows: list[dict], out: Callable[[str], None] = print) -> None:
    out("\n| 시나리오 | 기대 | 결과 | 예상대로 | 메모 |")
    out("|---|---|---|---|---|")
    for r in rows:
        out(f"| {r['sid']} {r['이름']} | {r['기대']} | {r['결과']} | {'O' if r['예상대로'] else 'X'} | {r['메모']} |")
    good = sum(r["예상대로"] for r in rows)
    out(f"\n예상대로 {good}/{len(rows)} (지원 {sum(r['결과'] == '성공' for r in rows)}개 성공)")
