"""손눈서브 도구 목록 -- 눈(보기)과 손(조작). 세 창구가 같은 목록을 쓴다:
로컬 LLM(basic_agent 도구) · Claude Code(mcp_bridge) · import(client.Sonnun).

실행은 확장(extension/background.js)이 하고, 사이트별 권한(끔/눈길/손길)도 확장이 실제 동작 직전에 검사한다.
여기는 이름·설명·인자 스키마와, 결과를 사람이 읽을 한 줄로 바꾸는 것만.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

Level = Literal["tab", "eye", "hand"]

_TAB = {"tab_id": {"type": "integer", "description": "대상 탭 (없으면 작업 탭 — 사용자가 일을 시킨 탭)"}}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    level: Level                       # tab: 탭 목록·전환 / eye: 보기 / hand: 조작


def _obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": {**(props or {}), **_TAB}, "required": required or []}


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("list_tabs", "열린 탭 목록 (tab_id·제목·주소·권한)",
             {"type": "object", "properties": {}, "required": []}, "tab"),
    ToolSpec("get_page", "페이지 보기: 제목·주소, 누르거나 입력할 수 있는 요소 목록(ref), 본문 앞부분. iframe·shadow DOM 안 요소도 "
             "포함(프레임 표시). 클릭·입력 전에 먼저 부른다. 페이지가 바뀌면 ref 도 바뀌니 다시 부른다",
             _obj({"max_elements": {"type": "integer", "description": "요소 최대 개수 (기본 40)"}}), "eye"),
    ToolSpec("find", "이름(글자)으로 요소 찾기 — 화면 밖·프레임·shadow DOM 까지 페이지 전체에서. get_page 목록에 원하는 것이 없을 때. "
             "찾은 ref 로 바로 click·type_text 하면 그 위치로 스크롤된다",
             _obj({"text": {"type": "string", "description": "요소 이름에 든 글자 (예: 결제, 다음, S9)"}}, ["text"]), "eye"),
    ToolSpec("read_text", "본문 이어 읽기 (get_page 의 본문이 잘렸을 때). start 글자부터 length 글자. "
             "iframe 글은 frame_id (get_page 의 프레임 번호)",
             _obj({"start": {"type": "integer"}, "length": {"type": "integer", "description": "기본 2000, 최대 4000"},
                   "frame_id": {"type": "integer", "description": "iframe 번호 (없으면 맨 위 문서)"}},
                  ["start"]), "eye"),
    ToolSpec("get_selection", "사용자가 페이지에서 드래그해 선택한 글", _obj(), "eye"),
    ToolSpec("look", "화면을 그림으로 보고 질문에 답 받기 (로컬 비전 모델, 보이는 부분만). 요소 목록·본문에 안 나오는 것 — "
             "canvas·그림·차트·아이콘만 있는 버튼·배치·색 — 을 확인할 때만. 누르기는 여전히 ref 로",
             _obj({"question": {"type": "string", "description": "화면에서 알고 싶은 것"}}, ["question"]), "eye"),
    ToolSpec("navigate", "탭에서 주소 열기 (http/https)", _obj({"url": {"type": "string"}}, ["url"]), "hand"),
    ToolSpec("click", "요소 누르기 (ref 는 get_page·find 결과의 ref). 알림창(alert)은 기록해 돌려주고, 확인창(confirm)은 기본 '취소' — "
             "확인으로 답해야 하면 accept_dialog=true (사용자 승인을 거침). 새 탭이 열리면 new_tabs 로 알려 준다. "
             "결제·주문·삭제처럼 되돌리기 어려운 버튼은 사용자가 패널에서 허용해야 눌린다",
             _obj({"ref": {"type": "string"}, "accept_dialog": {"type": "boolean"},
                   "dialog_text": {"type": "string", "description": "prompt 창에 넣을 글 (accept_dialog 일 때)"}}, ["ref"]), "hand"),
    ToolSpec("type_text", "입력 칸에 글 넣기. submit=true 면 넣은 뒤 Enter (검색 등). select 는 보이는 선택지 글자를 text 로. "
             "비밀번호 칸은 거부됨",
             _obj({"ref": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
                  ["ref", "text"]), "hand"),
    ToolSpec("wait_for", "글자가 나타날 때까지 기다리기 (gone=true 면 사라질 때까지). 누른 뒤 결과가 늦게 뜨는 페이지에서. 최대 30초",
             _obj({"text": {"type": "string"}, "gone": {"type": "boolean"},
                   "timeout_sec": {"type": "integer", "description": "기본 10"}}, ["text"]), "eye"),
    ToolSpec("press_key", "특수 키 누르기: Enter·Escape·Tab·ArrowDown/Up/Left/Right·Backspace·Delete·Home·End·PageDown/Up·Space. "
             "ref 가 있으면 그 요소에, 없으면 지금 초점에 (모달 닫기 Escape, 드롭다운 방향키 등)",
             _obj({"key": {"type": "string"}, "ref": {"type": "string"}}, ["key"]), "hand"),
    ToolSpec("hover", "요소 위에 마우스 올리기 (올려야 펼쳐지는 메뉴). 펼친 뒤 get_page·find 로 다시 본다",
             _obj({"ref": {"type": "string"}}, ["ref"]), "hand"),
    ToolSpec("ask_user", "사용자에게 직접 해 달라고 요청하고 기다리기 — CAPTCHA·로그인·본인 인증·비밀번호처럼 에이전트가 하면 안 되거나 "
             "못 하는 일. 패널에 뜨고 사용자가 '다 했어요'를 누르면 이어서 한다",
             {"type": "object", "properties": {"message": {"type": "string"}, "timeout_sec": {"type": "integer"}},
              "required": ["message"]}, "tab"),
    ToolSpec("scroll", "페이지를 한 화면 위/아래로", _obj({"direction": {"type": "string", "enum": ["down", "up"]}},
                                                     ["direction"]), "hand"),
    ToolSpec("go_back", "뒤로 가기", _obj(), "hand"),
    ToolSpec("switch_tab", "작업 탭을 다른 탭으로 바꾸고 앞에 띄우기", {"type": "object", "properties": {"tab_id": {"type": "integer"}},
                                            "required": ["tab_id"]}, "tab"),
]}


def check_args(name: str, args: dict) -> str | None:
    """빠진 필수 인자가 있으면 모델이 고칠 수 있게 기대 스키마를 붙인 메시지 (REF/LLM.md 함정 '인자 이름 추측')."""
    spec = TOOLS[name]
    missing = [k for k in spec.schema.get("required", []) if k not in args or args[k] in (None, "")]
    unknown = [k for k in args if k not in spec.schema.get("properties", {})]
    if not missing and not unknown:
        return None
    parts = []
    if missing:
        parts.append(f"빠진 인자 {missing}")
    if unknown:
        parts.append(f"모르는 인자 {unknown}")
    return f"{name}: {', '.join(parts)} — 인자: {list(spec.schema.get('properties', {}))}"


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


def summarize(name: str, args: dict, result: dict) -> str:
    """도구 결과 -> 사람이 읽을 한 줄 (대화의 단계 표시와 활동 기록에 같이 쓴다)."""
    if "error" in result:
        return f"{_LABEL.get(name, name)} 실패: {result['error']}"
    r = result
    if name == "list_tabs":
        return f"탭 목록 보기 ({r.get('total', len(r.get('tabs', [])))}개)"
    if name == "get_page":
        nf = len(r.get("frames", []))
        return (f"페이지 보기: {(r.get('title') or _host(r.get('url', '')))[:40]} (요소 {len(r.get('elements', []))}개"
                + (f", 프레임 {nf}개)" if nf else ")"))
    if name == "read_text":
        return f"글 읽기 {r.get('start', 0)}~{r.get('start', 0) + len(r.get('text', ''))} / {r.get('text_length', 0)}자"
    if name == "get_selection":
        return f"선택한 글 보기 ({r.get('length', 0)}자)"
    if name == "look":
        return f"화면 보기: {str(args.get('question', ''))[:30]} → {str(r.get('answer', ''))[:40]}"
    if name == "find":
        return f"찾기: \"{args.get('text', '')}\" → {len(r.get('elements', []))}개"
    if name == "navigate":
        return f"주소 열기: {_host(r.get('url', args.get('url', '')))}"
    if name == "click":
        extra = "".join([" → 페이지 이동" if r.get("page_changed") else "",
                         f" → 새 탭 {len(r['new_tabs'])}개" if r.get("new_tabs") else "",
                         " (작업 탭을 새 탭으로)" if r.get("work_tab") else "",
                         "".join(f" ({d['type']}: {d['message'][:20]} → {d['answer']})" for d in r.get("dialogs") or [])])
        return f"누르기: {r.get('label') or '#' + str(args.get('ref'))}" + extra
    if name == "wait_for":
        verb = "사라짐" if args.get("gone") else "나타남"
        return f"기다리기: \"{args.get('text', '')}\" " + (f"{verb} ({r.get('waited_ms', 0) / 1000:.1f}초)" if r.get("met") else f"안 {verb} ({r.get('waited_ms', 0) / 1000:.0f}초)")
    if name == "press_key":
        return f"키 누르기: {args.get('key')}" + (" → 페이지 이동" if r.get("page_changed") else "")
    if name == "hover":
        return f"마우스 올리기: {r.get('label') or '#' + str(args.get('ref'))}"
    if name == "ask_user":
        return "사용자에게 요청: " + ("완료" if r.get("done") else "시간 초과" if r.get("timeout") else "못 함") + f" — {str(args.get('message', ''))[:30]}"
    if name == "type_text":
        return (f"입력하기 ({r.get('length', 0)}자" + (", 한 글자씩" if r.get("method") == "keys" else "") + ")"
                + (" + Enter" if r.get("submitted") else ""))
    if name == "scroll":
        return f"스크롤 {'위' if args.get('direction') == 'up' else '아래'}로"
    if name == "go_back":
        return "뒤로 가기"
    if name == "switch_tab":
        return f"탭 바꾸기: {(r.get('title') or '')[:40]}"
    return name


def render_for_model(name: str, result: dict) -> str:
    """모델에게 보일 결과 글. 페이지는 JSON 대신 한 줄에 요소 하나로 줄인다 -- 로컬 모델 컨텍스트가 8,192 토큰이라
    (JSON 으로 넣으면 같은 페이지가 2배 넘게 길어진다). 두 세션(로컬·Claude)이 같은 글을 본다."""
    import json

    if "error" in result:
        return f"오류: {result['error']}"
    if name == "get_page":
        r = result
        lines = [f"tab_id: {r.get('tab_id')}", f"제목: {r.get('title', '')}", f"주소: {r.get('url', '')}",
                 f"요소 [ref] 종류 이름 (화면 근처 {len(r.get('elements', []))}개 / 전체 {r.get('elements_total', 0)}개):"]
        for e in r.get("elements", []):
            line = f"[{e['ref']}] {e.get('kind', '')} {e.get('label', '')}".rstrip()
            if e.get("frame_id"):
                line += f" (프레임 {e['frame_id']})"
            if e.get("options"):
                line += f" 선택지: {e['options']}"
            if "value" in e:
                line += f' = "{e["value"]}"'
            if "checked" in e:
                line += " (체크됨)" if e["checked"] else " (체크 안 됨)"
            if e.get("href"):
                line += f" → {e['href']}"
            lines.append(line)
        hidden_n = r.get("elements_total", 0) - len(r.get("elements", []))
        if hidden_n > 0:
            names = " · ".join(r.get("offscreen") or [])
            lines.append(f"(화면 밖에 요소 {hidden_n}개 더 있음 — 누르려면 find 로 이름을 찾는다"
                         + (f": {names}" if names else "") + ")")
        sc = r.get("scroll") or {}
        n, total = len(r.get("text", "")), r.get("text_length", 0)
        more = f", 이어서 read_text start={n}" if n < total else ""
        lines.append(f"본문 ({n}/{total}자{more}; 스크롤 {sc.get('y', 0)}/{sc.get('height', 0)}):")
        lines.append(r.get("text", ""))
        for f in r.get("frames", []):
            if f.get("hidden"):
                lines.append(f"\n프레임 {f['frame_id']}: ({f['hidden']})")
                continue
            fn, ft = len(f.get("text", "")), f.get("text_length", 0)
            more = f", 이어서 read_text frame_id={f['frame_id']} start={fn}" if fn < ft else ""
            lines.append(f"\n프레임 {f['frame_id']} ({_host(f.get('url', ''))} · {f.get('title', '')}) 본문 ({fn}/{ft}자{more}):")
            lines.append(f.get("text", ""))
        return "\n".join(lines)
    if name == "read_text":
        r = result
        end = r.get("start", 0) + len(r.get("text", ""))
        tail = f"\n(이어서 read_text start={end})" if r.get("more") else "\n(끝)"
        return f"{r.get('start', 0)}~{end} / {r.get('text_length', 0)}자\n{r.get('text', '')}{tail}"
    if name == "look":
        return f"화면을 본 답 (비전 모델): {result.get('answer', '')}"
    if name == "find":
        els = result.get("elements", [])
        if not els:
            return f'"{result.get("query")}" 이(가) 든 요소 없음'
        out = [f'"{result.get("query")}" 찾음 {len(els)}개 (전체 {result.get("matched", len(els))}개):']
        for e in els:
            out.append(f"[{e['ref']}] {e.get('kind', '')} {e.get('label', '')}" + (f" (프레임 {e['frame_id']})" if e.get("frame_id") else ""))
        return "\n".join(out)
    if name == "list_tabs":
        tabs = result.get("tabs", [])
        has_work = any("work" in t for t in tabs)

        def mark(t: dict) -> str:
            if has_work:
                return " (작업 탭)" if t.get("work") else ""
            return " (지금 탭)" if t.get("active") else ""
        return "\n".join(f"tab_id {t['tab_id']}{mark(t)} [{t.get('level')}] "
                         f"{t.get('title', '')[:50]} {t.get('url', '')[:80]}" for t in tabs) or "열린 탭 없음"
    return json.dumps(result, ensure_ascii=False)


_LABEL = {"list_tabs": "탭 목록 보기", "get_page": "페이지 보기", "look": "화면 보기", "find": "찾기", "wait_for": "기다리기",
          "press_key": "키 누르기", "hover": "마우스 올리기", "ask_user": "사용자에게 요청", "read_text": "글 읽기", "get_selection": "선택한 글 보기",
          "navigate": "주소 열기", "click": "누르기", "type_text": "입력하기", "scroll": "스크롤", "go_back": "뒤로 가기",
          "switch_tab": "탭 바꾸기"}
