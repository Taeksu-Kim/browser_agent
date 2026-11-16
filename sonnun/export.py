"""대화에서 에이전트가 성공한 조작을 다시 실행하는 Python 스크립트로 내보내기.

처음 이야기한 흐름 "탐색은 에이전트로, 반복은 스크립트로"의 마지막 단계. ref 는 그때뿐인 번호라 **요소 이름**으로 바꾼다.

- ``sonnun``: 손눈서브(서버+확장)로 내 Chrome 에서 다시 실행 — 로그인 상태·권한·승인이 그대로 적용된다.
- ``playwright``: 손눈서브 없이 Playwright 단독 실행 — 새 브라우저라 로그인 상태가 없다. iframe 안 요소는 손으로 고쳐야 한다.

보기만 한 도구(get_page·find·look·read_text 등)와 실패한 조작은 뺀다 (실패는 주석으로 남김). 탭 번호로 한 switch_tab 도
다음 실행에서는 의미가 없어 주석으로만 남긴다.
"""
from __future__ import annotations

import json
from typing import Literal

Format = Literal["sonnun", "playwright"]
ACTIONS = ("navigate", "click", "type_text", "press_key", "hover", "scroll", "go_back", "wait_for", "switch_tab", "ask_user")


def _q(v: object) -> str:
    return json.dumps(v, ensure_ascii=False)


def steps(activity: list[dict]) -> tuple[str, list[dict]]:
    """활동 기록(오래된 것부터) -> (시작 주소, 조작 목록)."""
    start = next((a["url"] for a in activity if a.get("url")), "")
    return start, [a for a in activity if a["tool"] in ACTIONS]


def to_script(activity: list[dict], fmt: Format = "sonnun", *, title: str = "") -> str:
    start, acts = steps(activity)
    if fmt == "playwright":
        return _playwright(start, acts, title)
    return _sonnun(start, acts, title)


def _label(a: dict) -> str:
    return (a.get("detail") or {}).get("label") or ""


def _sonnun(start: str, acts: list[dict], title: str) -> str:
    out = [f'"""손눈서브 대화에서 내보낸 스크립트{": " + title if title else ""}',
           "",
           "손눈서브 서버(python -m sonnun serve)와 Chrome 확장이 켜져 있어야 한다. 사이트 권한·멈춤·승인이 그대로 적용된다.",
           '    python 이_파일.py            # SONNUN_URL 로 서버 주소를 바꿀 수 있음',
           '"""',
           "import os",
           "import sys",
           "",
           "sys.path.insert(0, " + _q(str(__import__("pathlib").Path(__file__).resolve().parent.parent)) + ")",
           "from sonnun.client import Sonnun  # noqa: E402",
           "from sonnun.replay import click_label, hover_label, type_label  # noqa: E402",
           "",
           'b = Sonnun(os.environ.get("SONNUN_URL", "http://127.0.0.1:8771"))']
    if start:
        out.append(f"b.navigate(url={_q(start)})")
    for a in acts:
        args, lab = a["args"], _label(a)
        line = None
        if not a["ok"]:
            out.append(f"# (실패해서 뺌) {a['summary']}")
            continue
        t = a["tool"]
        if t == "navigate":
            line = f"b.navigate(url={_q(args.get('url'))})"
        elif t == "click" and lab:
            extra = ", accept_dialog=True" if args.get("accept_dialog") else ""
            line = f"click_label(b, {_q(lab)}{extra})"
        elif t == "type_text" and lab:
            line = f"type_label(b, {_q(lab)}, {_q(args.get('text', ''))}" + (", submit=True)" if args.get("submit") else ")")
        elif t == "hover" and lab:
            line = f"hover_label(b, {_q(lab)})"
        elif t == "press_key":
            line = f"b.press_key(key={_q(args.get('key'))})"
        elif t == "scroll":
            line = f"b.scroll(direction={_q(args.get('direction', 'down'))})"
        elif t == "go_back":
            line = "b.go_back()"
        elif t == "wait_for":
            line = f"b.wait_for(text={_q(args.get('text'))}" + (", gone=True" if args.get("gone") else "") + \
                   f", timeout_sec={int(args.get('timeout_sec') or 10)})"
        elif t == "ask_user":
            line = f"b.ask_user(message={_q(args.get('message'))})"
        if line:
            out.append(line)
        else:
            out.append(f"# (다시 실행할 수 없어 뺌) {a['summary']}")
    out.append('print("끝:", b.get_page()["url"])')
    return "\n".join(out) + "\n"


def _playwright(start: str, acts: list[dict], title: str) -> str:
    out = [f'"""손눈서브 대화에서 내보낸 Playwright 스크립트{": " + title if title else ""}',
           "",
           "손눈서브 없이 단독 실행. 새 브라우저라 로그인 상태가 없다 — 로그인이 필요하면 storage_state 를 붙인다.",
           "iframe 안 요소는 frame_locator 로 손봐야 한다 (주석 표시). HEADLESS=1 이면 화면 없이.",
           '"""',
           "import os",
           "",
           "from playwright.sync_api import sync_playwright",
           "",
           "",
           "def el(page, label):",
           '    """요소 이름으로 찾기: 버튼 → 링크 → 라벨 → placeholder → 글자"""',
           "    for loc in (page.get_by_role(\"button\", name=label, exact=True), page.get_by_role(\"link\", name=label, exact=True),",
           "                page.get_by_label(label, exact=True), page.get_by_placeholder(label, exact=True),",
           "                page.get_by_text(label, exact=True)):",
           "        if loc.count():",
           "            return loc.first",
           '    raise LookupError(f"{label!r} 요소를 찾지 못함")',
           "",
           "",
           "with sync_playwright() as p:",
           '    browser = p.chromium.launch(headless=os.environ.get("HEADLESS") == "1",',
           '                                executable_path=os.environ.get("CHROMIUM_PATH") or None)   # 브라우저 경로를 바꿀 때',
           "    page = browser.new_page()",
           '    page.on("dialog", lambda d: d.dismiss())   # 확인창은 기본 취소 (손눈서브와 같게)']
    if start:
        out.append(f"    page.goto({_q(start)})")
    for a in acts:
        args, lab, d = a["args"], _label(a), a.get("detail") or {}
        if not a["ok"]:
            out.append(f"    # (실패해서 뺌) {a['summary']}")
            continue
        t = a["tool"]
        note = "  # TODO: iframe 안 — frame_locator 필요" if d.get("frame_id") else ""
        line = None
        if t == "navigate":
            line = f"page.goto({_q(args.get('url'))})"
        elif t == "click" and lab:
            line = f"el(page, {_q(lab)}).click()"
        elif t == "type_text" and lab:
            line = f"el(page, {_q(lab)}).fill({_q(args.get('text', ''))})"
            if args.get("submit"):
                out.append(f"    {line}{note}")
                line = f"el(page, {_q(lab)}).press(\"Enter\")"
        elif t == "hover" and lab:
            line = f"el(page, {_q(lab)}).hover()"
        elif t == "press_key":
            key = {"Space": " "}.get(args.get("key"), args.get("key"))
            line = f"page.keyboard.press({_q(key)})"
        elif t == "scroll":
            line = f"page.mouse.wheel(0, {'-' if args.get('direction') == 'up' else ''}600)"
        elif t == "go_back":
            line = "page.go_back()"
        elif t == "wait_for":
            state = "hidden" if args.get("gone") else "visible"
            line = f"page.get_by_text({_q(args.get('text'))}).first.wait_for(state={_q(state)}, timeout={int(args.get('timeout_sec') or 10) * 1000})"
        elif t == "ask_user":
            line = f"input({_q('사람이 할 일: ' + str(args.get('message')) + ' — 다 하면 Enter')})"
        if line:
            out.append(f"    {line}{note}")
        else:
            out.append(f"    # (다시 실행할 수 없어 뺌) {a['summary']}")
    out += ['    page.wait_for_load_state()', '    print("끝:", page.url)', "    browser.close()"]
    return "\n".join(out) + "\n"
