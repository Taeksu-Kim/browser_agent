"""E2E 점검: 실제 Chromium 에 손눈서브 확장을 올리고, 가짜 웹페이지·가짜 LLM 으로 전 경로를 눌러 본다.

    python tools/browser_check.py                 # 모델은 가짜 (로컬 LLM 가짜 스크립트 + Claude 가짜 runner)
    python tools/browser_check.py --real-claude   # Claude Code 세션은 진짜 claude -p 로 1턴 (비용 약 $0.1, 테스트 페이지만 전송)

임시 DB·임시 포트·임시 프로필로 띄우고 끝나면 정리한다 (실제 data/ 는 건드리지 않음).
확인: 확장 연결 / 눈·손 도구 10종 / 권한(끔·눈길·손길·기본값)·멈춤 / 비밀번호 거부 / 낡은 ref / 같은 입력 2회 /
대화(로컬·Claude) / 같은 대화 이어서 / 동시 실행 거부 / 중지 / 화면 연결 끊김 / 모델 꺼짐 / MCP 다리 / CLI /
서버 재시작 후 대화 유지·확장 재연결 / 사이드 패널 화면 (폭 2종·다크·콘솔 오류·깨진 값·레이아웃).
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from sonnun.app import create_app  # noqa: E402
from sonnun.chat import ClaudeBackend  # noqa: E402
from sonnun.client import Sonnun, SonnunError  # noqa: E402

REAL_CLAUDE = "--real-claude" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="sonnun_check_"))
SHOTS = TMP / "shots"
SHOTS.mkdir()
PY = sys.executable

results: list[bool] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail not in ("", None) else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(fn, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step)
    return False


# ---------------------------------------------------------------- 가짜 웹페이지
class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass


web = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(ROOT / "tools" / "fixtures")))
threading.Thread(target=web.serve_forever, daemon=True).start()
WEB = f"http://127.0.0.1:{web.server_port}/"


# ---------------------------------------------------------------- 가짜 로컬 LLM (react 행동 JSON 을 순서대로)
class ScriptLLM:
    def __init__(self) -> None:
        self.script: list = []
        self.calls: list[tuple[str, str]] = []
        self.delay = 0.0

    def complete(self, system: str, user: str, *, schema=None) -> str:
        self.calls.append((system, user))
        if self.delay:
            time.sleep(self.delay)
        item = self.script.pop(0) if self.script else {"action": "finish", "final": "(스크립트 끝)"}
        if callable(item):
            item = item(user)
        return json.dumps(item, ensure_ascii=False)


llm = ScriptLLM()
llm_up = {"ok": True}


def ping():
    return (True, "") if llm_up["ok"] else (False, "로컬 LLM 이 꺼져 있습니다 (가짜). vLLM(:8002)을 켜 주세요")


# Whisper 엔진용 가짜 마이크: 조용 0.6초 → 말소리 비슷한 소리 1.2초 → 조용 3초 (반복 재생됨)
def write_fake_speech(path: Path) -> None:
    import math
    import struct
    import wave
    sr, out = 16000, []
    for i in range(int(sr * 4.8)):
        t = i / sr
        if 0.6 <= t < 1.8:
            f0 = 140 + 40 * math.sin(2 * math.pi * 1.5 * t)
            env = 0.5 + 0.5 * math.sin(2 * math.pi * 4 * t)          # 음절처럼 4Hz 로 커졌다 작아짐
            v = env * sum(math.sin(2 * math.pi * f0 * k * t) / k for k in range(1, 6)) * 0.25
        else:
            v = 0.0
        out.append(struct.pack("<h", int(max(-1, min(1, v)) * 32767)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(out))


FAKE_WAV = TMP / "fake_speech.wav"
write_fake_speech(FAKE_WAV)
stt_calls: list[int] = []
from sonnun.stt import FuncStt  # noqa: E402
fake_stt = FuncStt(lambda pcm: stt_calls.append(len(pcm)) or {"text": "위스퍼로 받은 말", "ms": 12, "audio_s": round(len(pcm) / 32000, 2)})

claude_cmds: list[list[str]] = []
vision_calls: list[tuple[str, str]] = []


def fake_vision(image: str, question: str) -> dict:
    vision_calls.append((image, question))
    return {"answer": "캔버스에 4729 가 적혀 있음", "prompt_tokens": 1000}


def fake_claude(cmd, cwd, cancel):
    """claude -p 대신: stream-json 줄을 흉내 내고, 도구는 진짜 MCP 다리 함수로 서버에 보낸다."""
    claude_cmds.append(cmd)
    from sonnun.mcp_bridge import _call
    a = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["sonnun"]["args"]
    url, chat = a[a.index("--url") + 1], a[a.index("--chat") + 1]
    sid = cmd[cmd.index("--session-id" if "--session-id" in cmd else "--resume") + 1]
    yield json.dumps({"type": "system", "subtype": "init", "session_id": sid})
    yield json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__sonnun__get_page", "input": {}}]}})
    text, err = _call(url, chat, "get_page", {})
    yield json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": text}], "is_error": err}]}})
    yield json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                        "delta": {"type": "text_delta", "text": "페이지를 봤습니다."}}})
    yield json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "페이지를 봤습니다."}]}})
    yield json.dumps({"type": "result", "subtype": "success", "result": "페이지를 봤습니다.", "total_cost_usd": 0.01})


# ---------------------------------------------------------------- 손눈서브 서버
PORT = free_port()
SONNUN = f"http://127.0.0.1:{PORT}"
DB = TMP / "sonnun.db"


def start_server() -> tuple[uvicorn.Server, threading.Thread]:
    claude = ClaudeBackend(SONNUN) if REAL_CLAUDE else ClaudeBackend(SONNUN, runner=fake_claude, claude_bin="claude")
    app = create_app(DB, llm_factory=lambda: llm, llm_ping=ping, claude=claude, server_url=SONNUN, vision=fake_vision, stt=fake_stt)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", timeout_graceful_shutdown=2))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    wait_until(lambda: urllib.request.urlopen(SONNUN + "/api/status", timeout=2), 15)
    return srv, th


def stop_server(srv: uvicorn.Server, th: threading.Thread) -> None:
    srv.should_exit = True
    th.join(timeout=10)


def get(path: str):
    with urllib.request.urlopen(SONNUN + path, timeout=10) as r:
        return json.load(r)


def post(path: str, body: dict | None = None):
    req = urllib.request.Request(SONNUN + path, data=json.dumps(body or {}, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def post_status(path: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(SONNUN + path, data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def turn(cid: str, text: str, *, on_open=None) -> list[dict]:
    req = urllib.request.Request(f"{SONNUN}/api/chats/{cid}/messages", data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    events = []
    with urllib.request.urlopen(req, timeout=120) as r:
        if on_open:
            on_open(r)
        for raw in r:
            line = raw.decode().strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


srv, th = start_server()
b = Sonnun(SONNUN)
FALLBACK = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
EXT = str(ROOT / "extension")

with sync_playwright() as p:
    kw = dict(headless=True, args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                                   "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                   f"--use-file-for-fake-audio-capture={FAKE_WAV}"])
    try:
        ctx = p.chromium.launch_persistent_context(str(TMP / "profile"), channel="chromium", **kw)
    except Exception:  # noqa: BLE001
        ctx = p.chromium.launch_persistent_context(str(TMP / "profile"), executable_path=str(FALLBACK[-1]), **kw)
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    ext_id = sw.url.split("/")[2]

    def storage(**kv):
        sw.evaluate("kv => chrome.storage.local.set(kv)", kv)
        time.sleep(0.1)

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(WEB + "index.html")
    storage(server=SONNUN)
    check("확장이 서버에 붙음", wait_until(lambda: get("/api/status")["extension"]["connected"], 15), get("/api/status")["extension"])

    # ------------------------------------------------------------ 눈
    pg = b.get_page()
    labels = {e["label"]: e for e in pg["elements"]}
    check("get_page: 제목·주소", pg["title"] == "손눈 테스트 첫 페이지" and pg["url"].endswith("index.html"), pg["title"])
    check("get_page: 입력 칸 라벨(label for)", "검색어" in labels and labels["검색어"]["kind"] == "input:text", list(labels)[:8])
    check("get_page: 숨은·비활성 버튼 제외", "숨은 버튼" not in labels and "못 누르는 버튼" not in labels)
    pw = [e for e in pg["elements"] if e["kind"] == "input:password"]
    check("get_page: 비밀번호 칸 값 미노출", pw and "value" not in pw[0], pw)
    check("get_page: 본문 앞부분만 (긴 본문)", len(pg["text"]) == 1200 and pg["text_length"] > 4000, (len(pg["text"]), pg["text_length"]))
    pg2 = b.get_page()
    check("같은 페이지 두 번 보기: ref 유지", [e["ref"] for e in pg2["elements"]] == [e["ref"] for e in pg["elements"]])

    end = b.read_text(start=pg["text_length"] - 300)
    check("read_text: 끝까지 이어 읽기", "긴 본문 끝 표시" in end["text"] and end["more"] is False)

    # ------------------------------------------------------------ 손
    q_ref = labels["검색어"]["ref"]
    b.type_text(ref=q_ref, text="날씨")
    check("type_text: input 이벤트까지 (React 식 칸)", page.inner_text("#echo") == "입력값: 날씨", page.inner_text("#echo"))
    cnt = labels["눌린 횟수 0"]["ref"]
    b.click(ref=cnt)
    b.click(ref=cnt)
    check("click 같은 요소 2번 → 2번 눌림", page.inner_text("#counter") == "눌린 횟수 2", page.inner_text("#counter"))
    try:
        b.type_text(ref=pw[0]["ref"], text="secret")
        check("비밀번호 칸 입력 거부", False, "거부 안 됨")
    except SonnunError as e:
        check("비밀번호 칸 입력 거부", e.code == "refused" and page.input_value("#pw") == "", e)
    try:
        b.click(ref="9999")
        check("없는 ref → not_found", False)
    except SonnunError as e:
        check("없는 ref → not_found", e.code == "not_found", e)
    s = b.scroll(direction="down")
    check("scroll 아래로", s["y"] > 0, s)

    r = b.type_text(ref=q_ref, text="서울 날씨", submit=True)
    check("type_text submit → 검색 결과로 이동", "results.html?q=" in r["url"], r["url"])
    res = b.get_page()
    check("이동 후 페이지 보기", "검색 결과: 서울 날씨" in res["text"], res["text"][:40])
    try:
        b.click(ref=q_ref)
        check("이동 후 이전 ref → not_found", False)
    except SonnunError as e:
        check("이동 후 이전 ref → not_found", e.code == "not_found", e)
    back = b.go_back()
    check("go_back", back["url"].endswith("index.html"), back.get("url"))
    link = next(e for e in b.get_page()["elements"] if e["label"] == "두 번째 페이지")
    c = b.click(ref=link["ref"])
    check("링크 click → page_changed", c["page_changed"] and c["url"].endswith("page2.html"), c)
    page.evaluate("() => { const r = document.createRange(); r.selectNodeContents(document.getElementById('sel'));"
                  " const s = getSelection(); s.removeAllRanges(); s.addRange(r); }")
    sel = b.get_selection()
    check("get_selection: 사용자가 고른 글", sel["text"] == "드래그해서 고를 문장입니다.", sel)

    page_b = ctx.new_page()
    page_b.goto(WEB + "results.html?q=탭둘")
    tabs = b.list_tabs()
    check("list_tabs: 탭 2개", tabs["total"] == 2, [t["title"] for t in tabs["tabs"]])
    first = next(t for t in tabs["tabs"] if t["title"] == "두 번째 페이지")
    b.switch_tab(tab_id=first["tab_id"])
    check("switch_tab 후 기본 대상이 바뀜", b.get_page()["title"] == "두 번째 페이지")
    other = next(t for t in tabs["tabs"] if t["title"].startswith("검색 결과"))
    check("tab_id 로 다른 탭 보기", "탭둘" in b.get_page(tab_id=other["tab_id"])["text"])
    try:
        b.navigate(url="chrome://version")
        check("navigate: http 아닌 주소 거부", False)
    except SonnunError as e:
        check("navigate: http 아닌 주소 거부", e.code == "bad_args", e)
    blank = ctx.new_page()
    blank_id = next(t["tab_id"] for t in b.list_tabs()["tabs"] if t["url"] in ("about:blank", ""))
    try:
        b.get_page(tab_id=blank_id)
        check("빈 탭(about:blank) 보기 → blocked_page", False)
    except SonnunError as e:
        check("빈 탭(about:blank) 보기 → blocked_page", e.code == "blocked_page", e)
    nav = b.navigate(url=WEB + "index.html", tab_id=blank_id)
    check("빈 탭에서 navigate 는 가능", nav["url"].endswith("index.html"), nav)
    blank.close()
    page_b.close()

    # ------------------------------------------------------------ 권한·멈춤
    b.switch_tab(tab_id=first["tab_id"])
    storage(perms={"127.0.0.1": "eye"})
    ok_eye = b.get_page()["title"] == "두 번째 페이지"
    try:
        b.click(ref=b.get_page()["elements"][0]["ref"])
        check("눈길: 보기 O · 조작 X", False)
    except SonnunError as e:
        check("눈길: 보기 O · 조작 X", ok_eye and e.code == "permission", e)
    try:
        b.navigate(url=WEB + "index.html")
        check("눈길: 다른 주소로 떠나기 X", False)
    except SonnunError as e:
        check("눈길: 다른 주소로 떠나기 X", e.code == "permission", e)
    storage(perms={"127.0.0.1": "off"})
    try:
        b.get_page()
        check("끔: 보기 X", False)
    except SonnunError as e:
        check("끔: 보기 X", e.code == "permission", e)
    masked = b.list_tabs()["tabs"]
    check("끔: 탭 목록에서 제목·주소 가림", all(t["url"] == "" and "가려진" in t["title"] for t in masked), masked)
    storage(perms={}, defaultLevel="eye")
    try:
        b.scroll(direction="down")
        check("기본값 눈길: 목록에 없는 사이트 조작 X", False)
    except SonnunError as e:
        check("기본값 눈길: 목록에 없는 사이트 조작 X", e.code == "permission", e)
    storage(defaultLevel="hand", paused=True)
    try:
        b.list_tabs()
        check("멈춤: 모든 동작 거부", False)
    except SonnunError as e:
        check("멈춤: 모든 동작 거부", e.code == "paused", e)
    storage(paused=False)
    check("멈춤 해제 후 다시 됨", b.get_page()["title"] == "두 번째 페이지")

    # 하위 도메인 규칙: "localhost" 규칙은 app.localhost 에도, 더 구체적인 규칙이 이긴다 (*.localhost 는 127.0.0.1 로 감)
    SUB = WEB.replace("127.0.0.1", "app.localhost")
    b.navigate(url=SUB + "page2.html")
    storage(perms={"localhost": "off"})
    try:
        b.get_page()
        check("하위 도메인: localhost 끔 → app.localhost 도 끔", False)
    except SonnunError as e:
        check("하위 도메인: localhost 끔 → app.localhost 도 끔", e.code == "permission" and "app.localhost" in str(e), e)
    storage(perms={"localhost": "off", "app.localhost": "eye"})
    seen = b.get_page()["title"] == "두 번째 페이지"
    try:
        b.scroll(direction="down")
        check("하위 도메인: 더 구체적인 규칙이 이김 (app.localhost 눈길)", False)
    except SonnunError as e:
        check("하위 도메인: 더 구체적인 규칙이 이김 (app.localhost 눈길)", seen and e.code == "permission", e)
    storage(perms={"calhost": "off"})
    check("하위 도메인: 글자 끝만 같은 규칙(calhost)은 안 맞음", b.get_page()["title"] == "두 번째 페이지")
    storage(perms={})
    b.navigate(url=WEB + "page2.html")

    acts = get("/api/activity?limit=500")
    check("활동 기록: import 호출이 모두 남음 (실패 포함)", len(acts) >= 30 and all(a["source"] == "api" for a in acts)
          and any(not a["ok"] for a in acts), len(acts))

    # ------------------------------------------------------------ 대화 ② 로컬 (가짜 스크립트)
    page.goto(WEB + "index.html")
    page.bring_to_front()
    tab_id = next(t["tab_id"] for t in b.list_tabs()["tabs"] if t["title"] == "손눈 테스트 첫 페이지")

    def type_search(user: str) -> dict:
        m = re.search(r"\[(\d+)\] input:text 검색어", user)
        return {"action": "tool", "tool": "type_text", "args": {"ref": m.group(1) if m else "?", "text": "부산 날씨", "submit": True}}

    chat = post("/api/chats", {"backend": "local"})
    llm.script = [{"action": "tool", "tool": "get_page", "args": {"tab_id": tab_id}}, type_search,
                  {"action": "finish", "final": "부산 날씨를 검색했습니다."}]
    ev = turn(chat["id"], "검색창에 부산 날씨 검색해줘")
    kinds = [e["type"] for e in ev]
    check("로컬 대화: 도구 2번 → 답", kinds == ["tool", "tool_result", "tool", "tool_result", "text", "done"], ev if "error" in kinds else kinds)
    # page.url 은 다른 Playwright 호출이 있어야 갱신되는 캐시값 -- 페이지에 직접 묻는다
    searched = wait_until(lambda: "q=%EB%B6%80%EC%82%B0" in page.evaluate("location.href"), 5)
    check("로컬 대화: 실제로 검색됨", searched, (page.evaluate("location.href"), [e.get("summary") for e in ev if e["type"] == "tool_result"],
                                         "" if searched else (b.list_tabs(), b.get_page(tab_id=tab_id)["url"])))
    check("로컬 대화: 모델이 본 페이지 = 요소 목록 글 (JSON 아님)", "[" in llm.calls[1][1] and "input:text 검색어" in llm.calls[1][1])
    msgs = get(f"/api/chats/{chat['id']}")["messages"]
    check("로컬 대화: 저장 (사용자·도우미, 단계 2개)", [m["role"] for m in msgs] == ["user", "assistant"]
          and len(msgs[1]["steps"]) == 2 and msgs[1]["text"] == "부산 날씨를 검색했습니다.", msgs[1])
    local_acts = get(f"/api/activity?chat={chat['id']}")
    check("로컬 대화: 활동 기록에 출처 local·대화 id", len(local_acts) == 2 and all(a["source"] == "local" for a in local_acts))

    # 스크립트로 내보내기 -> 실제로 다시 실행
    def script(cid: str, fmt: str) -> str:
        with urllib.request.urlopen(f"{SONNUN}/api/chats/{cid}/script?format={fmt}", timeout=10) as r:
            return r.read().decode()
    sc = script(chat["id"], "sonnun")
    check("내보내기(sonnun): ref 대신 요소 이름으로", 'type_label(b, "검색어", "부산 날씨", submit=True)' in sc
          and "index.html" in sc and "ref=" not in sc, sc[-300:])
    page.goto(WEB + "page2.html")
    (TMP / "replay_sonnun.py").write_text(sc, encoding="utf-8")
    rp = subprocess.run([PY, str(TMP / "replay_sonnun.py")], capture_output=True, text=True, timeout=120,
                        env={**__import__("os").environ, "SONNUN_URL": SONNUN})
    check("내보낸 sonnun 스크립트 실행 → 같은 검색이 다시 됨", rp.returncode == 0
          and "q=%EB%B6%80%EC%82%B0" in page.evaluate("location.href"), (rp.stdout[-200:], rp.stderr[-300:]))
    pw = script(chat["id"], "playwright")
    (TMP / "replay_pw.py").write_text(pw, encoding="utf-8")
    rp = subprocess.run([PY, str(TMP / "replay_pw.py")], capture_output=True, text=True, timeout=120,
                        env={**__import__("os").environ, "HEADLESS": "1", "CHROMIUM_PATH": str(FALLBACK[-1])})
    check("내보낸 Playwright 스크립트 단독 실행 → 검색 결과 주소", rp.returncode == 0 and "results.html?q=" in rp.stdout,
          (rp.stdout[-200:], rp.stderr[-400:]))
    try:
        urllib.request.urlopen(f"{SONNUN}/api/chats/{post('/api/chats', {'backend': 'local'})['id']}/script", timeout=10)
        check("내보내기: 조작 없는 대화 → 404", False)
    except urllib.error.HTTPError as exc:
        check("내보내기: 조작 없는 대화 → 404", exc.code == 404, exc.code)
    llm.script = [{"action": "finish", "final": "네, 이어서 답합니다."}]
    turn(chat["id"], "방금 뭐 했어?")
    check("같은 대화 이어서: 이전 대화가 모델 입력에 들어감",
          "[이전 대화]" in llm.calls[-1][1] and "부산 날씨 검색해줘" in llm.calls[-1][1])

    # 동시 실행 거부 + 중지
    llm.delay, llm.script = 1.5, [{"action": "tool", "tool": "get_page", "args": {}},
                                  {"action": "tool", "tool": "get_page", "args": {}}, {"action": "finish", "final": "끝"}]
    holder: dict = {}
    t1 = threading.Thread(target=lambda: holder.update(ev=turn(chat["id"], "느린 작업")))
    t1.start()
    time.sleep(0.5)
    other = post("/api/chats", {"backend": "local"})
    code, body = post_status(f"/api/chats/{other['id']}/messages", {"text": "동시에"})
    check("다른 대화가 도는 중이면 409", code == 409, (code, body))
    time.sleep(1.5)
    post(f"/api/chats/{chat['id']}/stop")
    t1.join(15)
    kinds = [e["type"] for e in holder.get("ev", [])]
    check("중지: 취소 오류로 끝남, 도구 1번 이하", kinds[-1:] == ["error"] and holder["ev"][-1]["kind"] == "cancelled"
          and kinds.count("tool") <= 1, kinds)
    check("중지: 저장된 답에 '중지' 표시", "중지" in get(f"/api/chats/{chat['id']}")["messages"][-1]["error"])
    check("중지 후 실행 표시 해제", get("/api/status")["running"] is None)

    # 화면이 연결을 끊음 -> 턴이 멈추고 실행 표시가 풀림
    llm.script = [{"action": "tool", "tool": "get_page", "args": {}}] * 5
    req =urllib.request.Request(f"{SONNUN}/api/chats/{other['id']}/messages", data=json.dumps({"text": "끊길 작업"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=30)
    resp.readline()
    resp.close()
    check("화면이 끊으면 턴이 멈춤", wait_until(lambda: get("/api/status")["running"] is None, 15), get("/api/status")["running"])
    llm.delay = 0

    # 모델 꺼짐
    llm_up["ok"] = False
    st = get("/api/status")["backends"]["local"]
    code, body = post_status(f"/api/chats/{other['id']}/messages", {"text": "모델 꺼짐"})
    check("모델 꺼짐: 상태에 이유, 보내면 503", not st["ok"] and code == 503 and "vLLM" in body, (st, code))
    llm_up["ok"] = True

    # ------------------------------------------------------------ 대화 ① Claude Code
    page.bring_to_front()
    cchat = post("/api/chats", {"backend": "claude"})
    ev = turn(cchat["id"], "이 페이지에 뭐가 있어? 한 문장으로.")
    tr = [e for e in ev if e["type"] == "tool_result"]
    check("Claude 대화: 도구 결과 요약이 화면용으로 옴", tr and tr[0]["ok"] and tr[0]["summary"].startswith("페이지 보기"), tr)
    check("Claude 대화: 끝 (done)", ev and ev[-1]["type"] == "done", ev[-1:])
    cacts = get(f"/api/activity?chat={cchat['id']}")
    check("Claude 대화: MCP 다리 경유 활동이 출처 claude 로 남음", cacts and all(a["source"] == "claude" for a in cacts), cacts[:1])
    check("Claude 대화: 세션 시작 기록", get(f"/api/chats/{cchat['id']}")["chat"]["claude_started"] == 1)
    if not REAL_CLAUDE:
        turn(cchat["id"], "이어서")
        check("Claude 둘째 턴은 --resume", "--resume" in claude_cmds[-1] and "--session-id" not in claude_cmds[-1])
    else:
        ev2 = turn(cchat["id"], "방금 본 페이지 제목을 다시 말해줘. 도구 쓰지 말고.")
        check("진짜 Claude 둘째 턴: 앞 턴 기억", any("검색 결과" in (e.get("text") or "") for e in ev2 if e["type"] in ("text", "done")),
              [e for e in ev2 if e["type"] == "done"])

    # ------------------------------------------------------------ MCP 다리 (다른 MCP 클라이언트처럼)
    proc = subprocess.Popen([PY, "-m", "sonnun.mcp_bridge", "--url", SONNUN], cwd=ROOT, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    msgs_in = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
               {"jsonrpc": "2.0", "method": "notifications/initialized"},
               {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
               {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_page", "arguments": {}}},
               {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}]
    out, _ = proc.communicate("\n".join(json.dumps(m) for m in msgs_in) + "\n", timeout=60)
    replies = {r["id"]: r for r in map(json.loads, out.strip().splitlines())}
    check("MCP: 알림에는 답 없음, 요청 4개에 답", sorted(replies) == [1, 2, 3, 4], sorted(replies))
    check("MCP: 도구 16개", len(replies[2]["result"]["tools"]) == 16)
    txt = replies[3]["result"]["content"][0]["text"]
    check("MCP: get_page 결과 (요약 줄 + 요소 목록)", txt.startswith("요약: 페이지 보기") and "[" in txt, txt[:60])
    check("MCP: 모르는 도구 → isError", replies[4]["result"]["isError"] is True)

    # ------------------------------------------------------------ CLI
    llm.script = [{"action": "tool", "tool": "get_page", "args": {}}, {"action": "finish", "final": "CLI 로 봤습니다."}]
    cli = subprocess.run([PY, "-m", "sonnun", "run", "이 페이지 봐줘", "--url", SONNUN], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    check("CLI run: 진행·답 출력, 종료 0", cli.returncode == 0 and "CLI 로 봤습니다." in cli.stdout and "get_page" in cli.stdout,
          cli.stdout[-200:] + cli.stderr[-200:])
    n_chats = len(get("/api/chats"))

    # ------------------------------------------------------------ 플레이그라운드: iframe · shadow DOM · 시나리오
    from sonnun.demo import STEPS, find, run as run_demo
    page.bring_to_front()
    rows = run_demo(b, SONNUN, slow=0.4, out=lambda *_: None)
    check("플레이그라운드: 시나리오 모두 예상대로 (S18 승인은 건너뜀)",
          all(r["예상대로"] for r in rows) and len(rows) == len(STEPS),
          [f"{r['sid']}:{r['결과']}{'/' + r['메모'] if r['메모'] else ''}" for r in rows if not r["예상대로"]] or "")
    page.screenshot(path=str(SHOTS / "playground_done.png"))
    byid = {r["sid"]: r for r in rows}
    check("확인창: 기본 '취소'로 답하고 내용을 돌려줌 (S19)", "confirm '정말 기록을 지울까요?' → 취소" in byid["S19"]["메모"], byid["S19"])
    check("알림창: 막히지 않고 기록 (S20)", "alert '저장되었습니다' → 확인" in byid["S20"]["메모"], byid["S20"])
    check("새 탭: 클릭 결과로 알려 줌 (S21)", "새 탭 1개" in byid["S21"]["메모"], byid["S21"])
    check("새 탭: 탭 목록에 보임", any(t["title"] == "플레이그라운드 새 탭" for t in b.list_tabs()["tabs"]))
    check("동작 뒤 알림창 가로채기 해제 (사용자 창은 그대로)", page.evaluate("document.documentElement.dataset.sonnunUntil") == "0")
    # Playwright 는 자기 호출 중에만 이벤트를 처리한다 -> expect_event 로 기다린다 (sleep 으로 기다리면 창이 열린 채 남음)
    with page.expect_event("dialog", timeout=3000) as dlg:
        page.evaluate("() => setTimeout(() => alert('사용자 알림'), 0)")
    user_msg = dlg.value.message
    dlg.value.dismiss()
    check("사용자가 띄운 알림창은 손눈서브가 가로채지 않음", user_msg == "사용자 알림", user_msg)
    wt = b.wait_for(text="절대 안 나오는 글자", timeout_sec=1)
    check("wait_for: 시간 초과면 met=false (오류 아님)", wt["met"] is False and 1000 <= wt["waited_ms"] < 3000, wt)
    check("wait_for gone: 없는 글자는 바로 충족", b.wait_for(text="절대 안 나오는 글자", gone=True)["met"] is True)
    try:
        b.press_key(key="F13")
        check("press_key: 모르는 키 거부", False)
    except SonnunError as e:
        check("press_key: 모르는 키 거부", e.code == "bad_args", e)
    storage(trusted=True)
    check("진짜 입력 켜면 서버 상태에 보임", wait_until(lambda: get("/api/status")["extension"]["trusted"], 5))
    rows_t = run_demo(b, SONNUN, slow=0.4, out=lambda *_: None)
    check("진짜 입력(CDP): 모두 성공 (닫힌 shadow·진짜 입력만 버튼 포함, S18 승인은 건너뜀)",
          all(r["결과"] in ("성공", "건너뜀") for r in rows_t) and all(r["예상대로"] for r in rows_t),
          [f"{r['sid']}:{r['결과']}{'/' + r['메모'] if r['메모'] else ''}" for r in rows_t if r["결과"] not in ("성공", "건너뜀")] or "")
    brd = page.inner_text("#board")
    check("진짜 입력(CDP): 점수판에 '진짜 입력'으로 찍힘 (S1·S11·S15), 다른 출처 iframe(S8)은 가짜 이벤트",
          all(f"{sid} " in brd for sid in ("S1", "S11", "S15"))
          and re.search(r"S1 기본 버튼\n성공 \(진짜 입력", brd) and re.search(r"S11 진짜 입력만\n성공 \(진짜 입력", brd)
          and re.search(r"S15 [^\n]*\n성공 \(진짜 입력", brd) and re.search(r"S8 다른 출처 iframe\n성공 \(가짜 이벤트", brd),
          brd[:600])
    page.screenshot(path=str(SHOTS / "playground_trusted.png"))
    storage(trusted=False)
    wait_until(lambda: not get("/api/status")["extension"]["trusted"], 5)
    pgp = b.get_page(max_elements=80)
    byl = {e["label"]: e for e in pgp["elements"]}
    for name in ("S9 shadow 버튼", "S3 동의"):          # 페이지가 길어 화면 근처 밖일 수 있음 -> 스크롤하며 찾기
        if name not in byl:
            hit = find(b, name)
            if hit:
                byl[name] = hit
    b.navigate(url=SONNUN + "/playground/")
    time.sleep(0.6)
    check("플레이그라운드: iframe 요소에 프레임 번호, shadow 요소 표시",
          byl.get("S7 iframe 버튼", {}).get("frame_id") and byl.get("S8 다른 출처 버튼", {}).get("frame_id")
          and byl.get("S9 shadow 버튼", {}).get("shadow"), {k: v for k, v in byl.items() if k[:3] in ("S7 ", "S8 ", "S9 ")})
    check("플레이그라운드: 감싼 label 이름 (체크박스 'S3 동의')", "S3 동의" in byl, [k for k in byl if k.startswith("S3")])
    check("플레이그라운드: 닫힌 shadow DOM 버튼은 안 보임", not any(k.startswith("S10") for k in byl))
    check("플레이그라운드: 프레임 본문도 보임", any("localhost" in f.get("url", "") for f in pgp["frames"]), pgp["frames"])
    s8 = next(e for e in b.get_page(max_elements=80)["elements"] if e["label"] == "S8 다른 출처 버튼")   # 다시 연 페이지의 ref
    storage(perms={"localhost": "off"})
    hidden = b.get_page(max_elements=80)
    check("프레임 권한: 다른 출처 프레임 사이트가 끔이면 요소·본문 가림",
          not any(e["label"].startswith("S8") for e in hidden["elements"]) and any(f.get("hidden") for f in hidden["frames"]),
          hidden["frames"])
    try:
        b.click(ref=s8["ref"])
        check("프레임 권한: 가린 프레임의 옛 ref 로 누르기 거부", False)
    except SonnunError as e:
        check("프레임 권한: 가린 프레임의 옛 ref 로 누르기 거부", e.code == "permission", e)
    storage(perms={"localhost": "eye"})
    try:
        b.click(ref=s8["ref"])
        check("프레임 권한: 프레임 사이트가 눈길이면 그 안 클릭 거부 (위 페이지는 손길)", False)
    except SonnunError as e:
        check("프레임 권한: 프레임 사이트가 눈길이면 그 안 클릭 거부 (위 페이지는 손길)", e.code == "permission", e)
    b.navigate(url=SONNUN + "/playground/")
    time.sleep(0.6)
    near_labels = [e["label"] for e in b.get_page()["elements"]]
    fd = b.find(text="S9")
    check("find: 화면 밖 요소를 이름으로 (shadow DOM)", "S9 shadow 버튼" not in near_labels
          and [e["label"] for e in fd["elements"]] == ["S9 shadow 버튼"], (near_labels[-3:], fd["elements"]))
    b.click(ref=fd["elements"][0]["ref"])
    time.sleep(0.3)
    check("find 로 받은 ref 로 누르기 → 스크롤되어 눌림", "S9 열린 shadow DOM\n성공" in page.inner_text("#board"))
    fs = b.find(text="S12")
    check("find: 다른 출처 iframe 안 요소도", fs["elements"] and fs["elements"][0].get("frame_id"), fs["elements"])
    check("find: 없는 이름 → 0개", b.find(text="없는버튼xyz")["elements"] == [])
    storage(perms={"localhost": "off"})
    try:
        b.look(question="캔버스 숫자?")
        check("화면 보기: 끔 사이트 프레임이 화면에 있으면 캡처 거부", False)
    except SonnunError as e:
        check("화면 보기: 끔 사이트 프레임이 화면에 있으면 캡처 거부", e.code == "permission" and not vision_calls, e)
    storage(perms={})
    lk = b.look(question="캔버스에 적힌 숫자는?")
    img = vision_calls[-1][0] if vision_calls else ""
    check("화면 보기: 캡처가 비전 모델로 감 (JPEG, 너비 ≤1024), 답만 돌아옴",
          lk.get("answer", "").startswith("캔버스") and img.startswith("data:image/jpeg;base64,") and lk["width"] <= 1024
          and "image" not in lk, {k: v for k, v in lk.items() if k != "image"})
    look_act = [a for a in get("/api/activity?limit=5") if a["tool"] == "look"]
    check("화면 보기: 기록에 이미지 데이터 없음", look_act and "base64" not in json.dumps(look_act), look_act[:1])
    bg_page = ctx.new_page()
    bg_page.goto(WEB + "page2.html")
    bg_id = next(t["tab_id"] for t in b.list_tabs()["tabs"] if t["title"] == "두 번째 페이지" and t["active"])
    page.bring_to_front()
    try:
        b.look(question="?", tab_id=bg_id)
        check("화면 보기: 뒤에 있는 탭은 거부 (보이는 탭만)", False)
    except SonnunError as e:
        check("화면 보기: 뒤에 있는 탭은 거부 (보이는 탭만)", e.code == "bad_args", e)
    bg_page.close()
    b.navigate(url=SONNUN + "/playground/")
    time.sleep(0.6)
    fr = next(f for f in b.get_page()["frames"] if "localhost" in f.get("url", ""))
    rt = b.read_text(start=0, frame_id=fr["frame_id"])
    check("read_text frame_id: 프레임 글 읽기", "이 프레임의 출처" in rt["text"], rt["text"][:40])
    try:
        b.read_text(start=0, frame_id=99999)
        check("read_text 없는 프레임 → not_found", False)
    except SonnunError as e:
        check("read_text 없는 프레임 → not_found", e.code == "not_found", e)

    # ------------------------------------------------------------ 사이드 패널 화면
    page.goto(WEB + "index.html")          # 가짜 답("첫 페이지에는 …")과 실제 탭이 맞게
    win_id = sw.evaluate("async () => (await chrome.windows.getLastFocused({windowTypes: ['normal']})).id")
    with ctx.expect_page() as pinfo:
        sw.evaluate("([u]) => chrome.windows.create({url: u, type: 'popup', width: 420, height: 800})",
                    [f"chrome-extension://{ext_id}/sidepanel.html?window={win_id}"])
    panel = pinfo.value
    errors: list[str] = []
    panel.on("pageerror", lambda e: errors.append(str(e)))
    panel.on("console", lambda m: m.type == "error" and errors.append(m.text))
    panel.wait_for_load_state()
    panel.set_viewport_size({"width": 380, "height": 760})
    wait_until(lambda: panel.get_attribute("#conn", "data-state") == "ok", 10)
    check("패널: 연결됨 표시", panel.inner_text("#conn") == "연결됨")
    check("패널: 이 사이트 = 127.0.0.1, 손길 선택", panel.inner_text("#host") == "127.0.0.1"
          and panel.get_attribute("#levels [data-level=hand]", "aria-checked") == "true", panel.inner_text("#host"))
    panel.click("#levels [data-level=eye]")
    time.sleep(0.2)
    perms = sw.evaluate("async () => (await chrome.storage.local.get('perms')).perms")
    check("패널: 눈길 누르면 권한 저장 + '직접 정함'", perms == {"127.0.0.1": "eye"} and "직접 정함" in panel.inner_text("#levelHint"),
          (perms, panel.inner_text("#levelHint")))
    panel.click("#levels [data-level=hand]")
    check("패널: 대화 목록에 이전 대화들 (CLI·Claude 포함)", panel.locator("#chatSelect option").count() == n_chats + 1,
          panel.locator("#chatSelect option").count())
    panel.screenshot(path=str(SHOTS / "panel_empty_380.png"))
    panel.check("input[value=claude]")
    check("패널: Claude 고르면 외부 전송 안내", "Anthropic" in panel.inner_text("#sessionNote"))
    panel.check("input[value=local]")
    llm.script = [{"action": "tool", "tool": "get_page", "args": {"tab_id": tab_id}},
                  {"action": "finish", "final": "첫 페이지에는 **검색창**과 두 번째 페이지 링크가 있습니다.\n\n- 검색 입력 칸\n- `page2.html` 링크\n\n"
                                                 "| 항목 | 개수 |\n|---|---|\n| 링크 | 1 |\n\n<img src=x onerror=\"document.title='XSS'\"> 끝"}]
    panel.fill("#input", "이 페이지 뭐 있어?")
    panel.press("#input", "Enter")
    wait_until(lambda: "검색창과" in panel.inner_text("#messages"), 15)
    check("패널: 보낸 말·단계·답이 보임 (단계는 접힌 한 줄)", "이 페이지 뭐 있어?" in panel.inner_text("#messages")
          and "작업 1단계" in panel.inner_text("#messages") and "페이지 보기" in panel.text_content(".steps ol"),
          panel.inner_text("#messages")[-200:])
    msg = panel.locator(".msg.assistant .md").last
    check("패널: 답의 마크다운 (굵게·목록·코드·표)", msg.locator("strong").count() == 1 and msg.locator("li").count() == 2
          and msg.locator("code").count() == 1 and msg.locator("table td").count() == 2 and "**" not in msg.inner_text(),
          msg.inner_html()[:300])
    check("패널: 답 속 HTML 은 글자로만 (태그·스크립트 실행 없음)", panel.locator("#messages img").count() == 0
          and "<img" in msg.inner_text() and panel.title() != "XSS")
    panel.click(".msg.assistant .steps summary >> nth=-1")
    check("패널: 단계 줄을 누르면 펼쳐짐", panel.is_visible(".steps[open] ol li"))
    panel.click("#levelInfo")
    check("패널: ⓘ 누르면 끔·눈길·손길 설명", panel.is_visible("#levelHelp") and "클릭·입력·다른 주소로 이동은 막힙니다" in panel.inner_text("#levelHelp"))
    panel.screenshot(path=str(SHOTS / "panel_help_380.png"))
    panel.keyboard.press("Escape")
    check("패널: Esc 로 설명 닫힘", panel.is_hidden("#levelHelp"))
    wait_until(lambda: panel.is_visible("#send"), 10)
    panel.screenshot(path=str(SHOTS / "panel_chat_380.png"))
    panel.set_viewport_size({"width": 520, "height": 760})
    panel.screenshot(path=str(SHOTS / "panel_chat_520.png"))
    panel.emulate_media(color_scheme="dark")
    panel.screenshot(path=str(SHOTS / "panel_chat_dark.png"))
    panel.emulate_media(color_scheme="light")
    panel.click("#tabLog")
    wait_until(lambda: panel.locator("#activity li").count() > 5, 10)
    check("패널: 기록 탭에 활동 목록", "페이지 보기" in panel.inner_text("#activity"))
    check("패널: 기록의 주소는 읽을 수 있게 (%XX 인코딩 없음)", not re.search(r"%[0-9A-F]{2}", panel.inner_text("#activity")))
    panel.screenshot(path=str(SHOTS / "panel_log_520.png"))
    panel.click("#tabChat")
    body = panel.inner_text("body")
    check("패널: null·undefined·NaN 없음", not re.search(r"\b(null|undefined|NaN)\b", body))
    check("패널: 가로 스크롤 없음", panel.evaluate("document.documentElement.scrollWidth <= innerWidth"))

    LAYOUT_JS = (ROOT / "tools" / "layout_check.js").read_text(encoding="utf-8")
    for w in (380, 520):
        panel.set_viewport_size({"width": w, "height": 760})
        lay = panel.evaluate(LAYOUT_JS)
        check(f"패널 {w}px: 넘침·겹침·작은 클릭 대상 없음", not any(lay.values()), lay)

    # ------------------------------------------------------------ 음성 입력 (진짜 Google 인식 대신 가짜 인식기 — 화면 흐름만 확인)
    FAKE_SR = """(c) => {
      window.__sr = c;
      window.SpeechRecognition = window.webkitSpeechRecognition = class {
        start() {
          const cfg = window.__sr;
          setTimeout(() => {
            if (cfg.error) { this.onerror && this.onerror({ error: cfg.error }); this.onend && this.onend(); return; }
            const res = (t, fin) => { const r = [{ transcript: t }]; r.isFinal = fin; return r; };
            this.onresult && this.onresult({ results: [res(cfg.text.slice(0, 3), false)] });
            setTimeout(() => { this.onresult && this.onresult({ results: [res(cfg.text, true)] }); this.onend && this.onend(); }, 200);
          }, 100);
        }
        stop() {}
        abort() { this.onend && this.onend(); }
      };
      renderMic();
    }"""

    def speak(text: str = "", error: str = "") -> None:
        panel.evaluate(FAKE_SR, {"text": text, "error": error})
        panel.click("#mic")

    def n_user() -> int:
        return panel.locator(".msg.user").count()
    storage(voiceMode="preview", voiceEngine="google")
    n0 = n_user()
    llm.script = [{"action": "finish", "final": "음성으로 받은 요청을 처리했습니다."}]
    speak("이 페이지 요약해줘")
    shown = wait_until(lambda: "뒤 보냅니다" in panel.inner_text("#voiceBar"), 5)
    check("음성: 말이 끝나면 입력창에 글 + '잠깐 뒤 보냅니다'", shown and panel.input_value("#input") == "이 페이지 요약해줘",
          (panel.input_value("#input"), panel.inner_text("#voiceBar") if panel.is_visible("#voiceBar") else "(안 보임)"))
    panel.screenshot(path=str(SHOTS / "panel_voice_preview_380.png"))
    sent = wait_until(lambda: "음성으로 받은 요청을 처리했습니다" in panel.inner_text("#messages"), 15)
    check("음성(잠깐 보여 주고 보내기): 1.5초 뒤 알아서 보냄", sent and n_user() == n0 + 1, n_user() - n0)
    wait_until(lambda: panel.is_visible("#send"), 10)
    n0 = n_user()
    speak("보내면 안 되는 말")
    wait_until(lambda: "뒤 보냅니다" in panel.inner_text("#voiceBar"), 5)
    panel.keyboard.press("Escape")
    time.sleep(2.0)
    check("음성: 보내기 전 Esc 면 안 보냄 (글은 입력창에 남음)", n_user() == n0 and panel.input_value("#input") == "보내면 안 되는 말",
          (n_user() - n0, panel.input_value("#input")))
    speak("이어서 말함")
    wait_until(lambda: "뒤 보냅니다" in panel.inner_text("#voiceBar"), 5)
    panel.click("#input")
    time.sleep(2.0)
    check("음성: 보내기 전 입력창을 누르면 안 보냄 (앞 글에 이어 붙음)", n_user() == n0
          and panel.input_value("#input") == "보내면 안 되는 말 이어서 말함", panel.input_value("#input"))
    panel.fill("#input", "")
    storage(voiceMode="fill")
    speak("넣기만 하는 말")
    wait_until(lambda: panel.input_value("#input") == "넣기만 하는 말", 5)
    time.sleep(2.0)
    check("음성(입력창에만): 넣고 보내지 않음", n_user() == n0 and panel.input_value("#input") == "넣기만 하는 말")
    panel.fill("#input", "")
    storage(voiceMode="auto")
    llm.script = [{"action": "finish", "final": "바로 받았습니다."}]
    speak("바로 보내는 말")
    sent = wait_until(lambda: "바로 받았습니다" in panel.inner_text("#messages"), 15)
    check("음성(바로 보내기): 말이 끝나면 곧바로 보냄", sent and n_user() == n0 + 1, n_user() - n0)
    wait_until(lambda: panel.is_visible("#send"), 10)
    storage(voiceMode="preview")
    speak(error="network")
    wait_until(lambda: "인터넷" in panel.inner_text("#voiceBar"), 5)
    check("음성: 인터넷 없으면 이유를 알려 줌", "인터넷" in panel.inner_text("#voiceBar"), panel.inner_text("#voiceBar"))
    speak(error="not-allowed")
    wait_until(lambda: panel.is_visible("#voiceAllow"), 5)
    check("음성: 마이크 허용 전이면 '마이크 허용하기' 안내", panel.is_visible("#voiceAllow") and "허용" in panel.inner_text("#voiceBar"))
    panel.screenshot(path=str(SHOTS / "panel_voice_denied_380.png"))
    with ctx.expect_page() as minfo:
        panel.click("#voiceAllow")
    mpage = minfo.value
    mpage.wait_for_load_state()
    wait_until(lambda: mpage.is_visible("#micAllow") or "허용됨" in mpage.inner_text("#micState"), 5)
    check("음성: '마이크 허용하기' → 설정 화면의 음성 입력 칸", "options.html#mic" in mpage.url
          and mpage.is_checked("input[name=voiceMode][value=preview]"), mpage.url)
    mpage.check("input[name=voiceMode][value=fill]")
    time.sleep(0.2)
    check("음성: 설정 화면에서 마이크 모드 고르기", sw.evaluate("async () => (await chrome.storage.local.get('voiceMode')).voiceMode") == "fill")
    mpage.screenshot(path=str(SHOTS / "options_voice_760.png"), full_page=True)
    mpage.check("input[name=voiceEngine][value=whisper]")
    wait_until(lambda: "음성 서버 켜짐" in mpage.inner_text("#sttState"), 8)
    check("음성: 설정 화면에서 Whisper 고르면 음성 서버 상태", "음성 서버 켜짐" in mpage.inner_text("#sttState"), mpage.inner_text("#sttState"))
    mpage.screenshot(path=str(SHOTS / "options_voice_whisper_760.png"), full_page=True)
    mpage.close()
    if panel.is_visible("#voiceCancel"):
        panel.click("#voiceCancel")

    # Whisper: 가짜 마이크(말소리 1.2초 뒤 조용) → 패널이 말 끝을 스스로 잡아 /api/stt 로 → 받아적은 글
    panel.evaluate("delete window.SpeechRecognition; delete window.webkitSpeechRecognition")
    storage(voiceEngine="whisper", voiceMode="fill")
    panel.evaluate("refreshStatus()")
    panel.fill("#input", "")
    n0 = n_user()
    panel.click("#mic")
    got = wait_until(lambda: panel.input_value("#input") == "위스퍼로 받은 말", 15)
    secs = [round(n / 32000, 1) for n in stt_calls]
    check("음성(Whisper): 말을 멈추면 녹음이 끝나 받아적은 글이 들어옴 (녹음 1~8초)", got and len(stt_calls) == 1 and 1.0 <= secs[0] <= 8.0,
          (panel.input_value("#input"), secs, panel.inner_text("#voiceBar") if panel.is_visible("#voiceBar") else ""))
    check("음성(Whisper): 입력창에만 모드라 보내지 않음", n_user() == n0)
    panel.fill("#input", "")
    fake_stt.ok = False
    panel.evaluate("refreshStatus()")
    time.sleep(0.5)
    panel.click("#mic")
    wait_until(lambda: panel.is_visible("#voiceBar"), 5)
    check("음성(Whisper): 음성 서버가 꺼져 있으면 켜는 법", "stt_server.py" in panel.inner_text("#voiceBar"), panel.inner_text("#voiceBar"))
    panel.screenshot(path=str(SHOTS / "panel_voice_whisper_down_380.png"))
    fake_stt.ok = True
    panel.click("#voiceCancel")
    storage(voiceEngine="google", voiceMode="preview")

    # ------------------------------------------------------------ 작업 탭 고정 (턴 도중 사용자가 다른 탭으로 옮겨도)
    page.goto(WEB + "index.html")
    page.bring_to_front()
    wait_until(lambda: panel.inner_text("#host") == "127.0.0.1", 5)
    a_id = next(t["tab_id"] for t in b.list_tabs()["tabs"] if t["title"] == "손눈 테스트 첫 페이지")
    gate = threading.Event()

    def nav_after_gate(_user: str) -> dict:
        gate.wait(30)                      # 그사이 테스트가 사용자처럼 다른 탭을 연다
        return {"action": "tool", "tool": "navigate", "args": {"url": WEB + "results.html?q=work"}}
    n_calls = len(llm.calls)
    llm.script = [nav_after_gate, {"action": "tool", "tool": "list_tabs", "args": {}},
                  {"action": "finish", "final": "결과 페이지를 열었습니다."}]
    panel.click("#newChat")
    panel.fill("#input", "결과 페이지 열어줘")
    panel.press("#input", "Enter")
    wait_until(lambda: len(llm.calls) > n_calls, 10)
    st = get("/api/status")
    check("작업 탭: 패널에서 보내면 그 탭이 작업 탭", st.get("work_tab") == a_id, (st.get("work_tab"), a_id))
    other = ctx.new_page()                 # 사용자가 다른 탭으로 옮김
    other.goto(WEB + "page2.html")
    other.bring_to_front()
    shown = wait_until(lambda: panel.is_visible("#workTab") and "작업 중" in panel.inner_text("#workTab"), 8)
    check("작업 탭: 다른 탭을 보면 패널에 '에이전트는 다른 탭에서 작업 중'", shown,
          panel.inner_text("#workTab") if panel.is_visible("#workTab") else "(안 보임)")
    panel.screenshot(path=str(SHOTS / "panel_worktab_380.png"))
    gate.set()
    wait_until(lambda: "결과 페이지를 열었습니다" in panel.inner_text("#messages"), 15)
    a_url, o_url = page.evaluate("location.href"), other.evaluate("location.href")
    check("작업 탭: 에이전트는 일을 시킨 탭을 조작, 사용자가 보는 탭은 그대로",
          "results.html?q=work" in a_url and o_url.endswith("page2.html"), (a_url, o_url))
    check("작업 탭: 탭 목록에 '(작업 탭)' 표시 = 일을 시킨 탭", f"tab_id {a_id} (작업 탭)" in llm.calls[-1][1],
          [ln for ln in llm.calls[-1][1].splitlines() if "tab_id" in ln][:4])
    wait_until(lambda: not get("/api/status")["running"], 5)
    check("작업 탭: 턴이 끝나면 풀림", get("/api/status").get("work_tab") is None)
    wait_until(lambda: panel.is_hidden("#workTab"), 7)
    check("작업 탭: 턴이 끝나면 패널 안내 사라짐", panel.is_hidden("#workTab"))

    # 탭을 알려 주지 않은 턴(CLI·import) → 시작할 때 사용자가 보던 탭 (지금은 other)
    o_id = next(t["tab_id"] for t in b.list_tabs()["tabs"] if t["title"] == "두 번째 페이지" and t["active"])
    cw = post("/api/chats", {"backend": "local"})
    llm.script = [{"action": "tool", "tool": "get_page", "args": {}}, {"action": "finish", "final": "봤음"}]
    turn(cw["id"], "이 페이지 봐줘")
    check("작업 탭: tab_id 없이 보낸 턴은 시작할 때 보던 탭", "두 번째 페이지" in llm.calls[-1][1], llm.calls[-1][1][:80])

    # 누른 결과 새 탭이 앞에 열리면 작업 탭이 따라간다 → 그 탭을 닫으면 알기 쉬운 오류
    for old in [pg_ for pg_ in ctx.pages if "newtab.html" in pg_.evaluate("location.href")]:
        old.close()                         # 앞선 시연에서 열린 새 탭들 (아래에서 이번 새 탭만 집으려고)
    page.goto(SONNUN + "/playground/")
    page.bring_to_front()
    time.sleep(0.6)
    s21 = b.find(text="S21")["elements"][0]["ref"]

    def click_s21(_user: str) -> dict:
        return {"action": "tool", "tool": "click", "args": {"ref": s21}}
    llm.script = [click_s21, {"action": "tool", "tool": "get_page", "args": {}}, {"action": "finish", "final": "새 탭 봤음"}]
    ev = turn(cw["id"], "S21 눌러서 새 탭 봐줘", )
    res = [e for e in ev if e["type"] == "tool_result"]
    check("작업 탭: 누른 결과 앞에 열린 새 탭으로 옮겨 감", len(res) == 2 and "작업 탭을 새 탭으로" in res[0]["summary"]
          and "새 탭" in res[1]["summary"], [r["summary"] for r in res])
    def page_at(part: str):
        for pg_ in ctx.pages:
            try:
                if part in pg_.evaluate("location.href"):     # page.url 은 늦게 갱신되는 캐시
                    return pg_
            except Exception:  # noqa: BLE001
                pass
        return None
    wait_until(lambda: page_at("newtab.html"), 5)
    newtab = page_at("newtab.html")
    gate2 = threading.Event()

    def look_after_close(_user: str) -> dict:
        gate2.wait(30)
        return {"action": "tool", "tool": "get_page", "args": {}}
    llm.script = [{"action": "tool", "tool": "switch_tab", "args": {"tab_id": next(
        t["tab_id"] for t in b.list_tabs()["tabs"] if "newtab" in t["url"])}}, look_after_close,
        {"action": "finish", "final": "닫힘 확인"}]
    box_ev: dict = {}
    th_t = threading.Thread(target=lambda: box_ev.update(ev=turn(cw["id"], "새 탭 다시 봐줘")))
    th_t.start()
    wait_until(lambda: len([a for a in get(f"/api/activity?chat={cw['id']}&limit=50") if a["tool"] == "switch_tab"]) > 0, 10)
    newtab.close()                          # 사용자가 작업 탭을 닫음
    gate2.set()
    th_t.join(30)
    res = [e for e in box_ev.get("ev", []) if e["type"] == "tool_result"]
    check("작업 탭: 작업 탭이 닫히면 '작업하던 탭이 닫힘' 오류 (다른 탭을 건드리지 않음)",
          len(res) == 2 and not res[1]["ok"] and "작업하던 탭이 닫힘" in res[1]["summary"], [r["summary"] for r in res])
    other.close()
    page.bring_to_front()

    # ------------------------------------------------------------ 설정 화면 (사이트 권한 목록)
    def perms_now() -> dict:
        return sw.evaluate("async () => (await chrome.storage.local.get('perms')).perms || {}")
    storage(perms={})                       # 위 패널 점검이 남긴 127.0.0.1 규칙을 비우고 첫 사용 상태로
    with ctx.expect_page() as oinfo:
        panel.click("#openOptions")
    opt = oinfo.value
    oerr: list[str] = []
    opt.on("pageerror", lambda e: oerr.append(str(e)))
    opt.on("console", lambda m: m.type == "error" and oerr.append(m.text))
    opt.wait_for_load_state()
    check("설정 화면: 패널의 '설정 · 사이트 권한 목록'으로 열림", opt.url.startswith(f"chrome-extension://{ext_id}/options.html"), opt.url)
    opt.set_viewport_size({"width": 760, "height": 900})
    shown = wait_until(lambda: opt.is_visible(".empty-rules"), 5)
    check("설정 화면: 규칙 없으면 빈 안내 + 기본값 손길 경고", shown and "아직 따로 정한 사이트가 없습니다" in opt.inner_text(".empty-rules")
          and "건드리면 안 되는" in opt.inner_text("#defaultHint"), (opt.inner_text("body")[:300], oerr, perms_now()))
    wait_until(lambda: opt.inner_text("#serverState") == "연결됨", 8)
    check("설정 화면: 서버 연결 상태", opt.inner_text("#serverState") == "연결됨", opt.inner_text("#serverState"))
    opt.screenshot(path=str(SHOTS / "options_empty_760.png"))

    opt.fill("#addSite", "https://www.tossinvest.com/account?x=1")
    preview = opt.inner_text("#addMsg")
    opt.click("#addBtn")
    time.sleep(0.2)
    check("설정 화면: 주소를 통째로 넣으면 사이트 이름만 (www. 떼고), 기본 끔으로 추가",
          perms_now() == {"tossinvest.com": "off"} and "하위 주소" in preview, (perms_now(), preview))
    opt.fill("#addSite", "이건 주소 아님 !!")
    opt.click("#addBtn")
    time.sleep(0.2)
    check("설정 화면: 알아볼 수 없는 주소는 추가 안 함 + 안내", perms_now() == {"tossinvest.com": "off"}
          and "알아볼 수 없습니다" in opt.inner_text("#addMsg"), opt.inner_text("#addMsg"))
    opt.fill("#addSite", "company.com")
    opt.select_option("#addLevel", "eye")
    opt.click("#addBtn")
    opt.fill("#addSite", "approval.company.com")
    opt.select_option("#addLevel", "off")
    opt.press("#addSite", "Enter")
    time.sleep(0.2)
    rows = opt.locator("#rules li")
    order = [rows.nth(i).get_attribute("data-site") for i in range(rows.count())]
    check("설정 화면: 목록은 끔 → 눈길 → 손길 순", order == ["approval.company.com", "tossinvest.com", "company.com"], order)
    check("설정 화면: 하위 규칙은 '안의 예외'로 표시", "company.com(눈길) 안의 예외"
          in opt.inner_text("#rules li[data-site='approval.company.com']"), opt.inner_text("#rules li[data-site='approval.company.com']"))
    opt.click("#rules li[data-site='tossinvest.com'] [data-level=eye]")
    time.sleep(0.2)
    check("설정 화면: 목록에서 권한 바꾸기", perms_now().get("tossinvest.com") == "eye", perms_now())
    opt.click("#rules li[data-site='approval.company.com'] .del")
    time.sleep(0.2)
    check("설정 화면: 지우기 + 이제 적용될 규칙 안내", "approval.company.com" not in perms_now()
          and "company.com 규칙" in opt.inner_text("#addMsg"), opt.inner_text("#addMsg"))
    opt.fill("#addSite", "company.com")
    opt.select_option("#addLevel", "hand")
    check("설정 화면: 이미 있는 사이트면 '바뀝니다' 미리 알림", "지금 눈길" in opt.inner_text("#addMsg"), opt.inner_text("#addMsg"))
    opt.fill("#addSite", "")
    storage(perms={f"site{i:02d}.example.com": ["off", "eye", "hand"][i % 3] for i in range(9)}
            | {"아주-긴-하위-주소-이름이-들어간-사이트.subdomain.very-long-company-name-for-layout-test.co.kr": "off"})
    wait_until(lambda: opt.locator("#rules li").count() == 10, 5)
    check("설정 화면: 규칙이 많으면 개수·찾기 표시", opt.is_visible("#filter") and "끔 4" in opt.inner_text("#counts"),
          opt.inner_text("#counts") if opt.is_visible("#counts") else "(안 보임)")
    opt.fill("#filter", "site03")
    check("설정 화면: 목록에서 찾기", opt.locator("#rules li").count() == 1, opt.locator("#rules li").count())
    opt.fill("#filter", "없는이름")
    check("설정 화면: 찾기 결과 없음 안내", "든 사이트가 없습니다" in opt.inner_text("body"))
    opt.fill("#filter", "")
    opt.screenshot(path=str(SHOTS / "options_many_760.png"), full_page=True)
    LAYOUT_JS = (ROOT / "tools" / "layout_check.js").read_text(encoding="utf-8")
    for w in (760, 380):
        opt.set_viewport_size({"width": w, "height": 900})
        lay = opt.evaluate(LAYOUT_JS)
        check(f"설정 화면 {w}px: 넘침·겹침·작은 클릭 대상 없음", not any(lay.values()), lay)
        check(f"설정 화면 {w}px: 가로 스크롤 없음", opt.evaluate("document.documentElement.scrollWidth <= innerWidth"))
    opt.screenshot(path=str(SHOTS / "options_many_380.png"), full_page=True)
    opt.emulate_media(color_scheme="dark")
    opt.set_viewport_size({"width": 760, "height": 900})
    opt.screenshot(path=str(SHOTS / "options_dark_760.png"), full_page=True)
    opt.emulate_media(color_scheme="light")
    opt.click("#defaultLevels [data-level=eye]")
    time.sleep(0.2)
    check("설정 화면: 기본 권한 바꾸기", sw.evaluate("async () => (await chrome.storage.local.get('defaultLevel')).defaultLevel") == "eye")
    opt.click("#defaultLevels [data-level=hand]")
    opt.fill("#approvalTimeout", "5")
    opt.press("#approvalTimeout", "Tab")
    time.sleep(0.2)
    check("설정 화면: 승인 대기 시간은 30~600초로", opt.input_value("#approvalTimeout") == "30"
          and sw.evaluate("async () => (await chrome.storage.local.get('approvalTimeoutSec')).approvalTimeoutSec") == 30)
    opt.fill("#approvalTimeout", "120")
    opt.press("#approvalTimeout", "Tab")
    opt.check("#trusted")
    time.sleep(0.3)
    check("설정 화면: 진짜 입력 켜기 → 서버도 앎", get("/api/status")["extension"].get("trusted") is True)
    opt.uncheck("#trusted")
    body_o = opt.inner_text("body")
    check("설정 화면: null·undefined·NaN 없음", not __import__("re").search(r"\b(null|undefined|NaN)\b", body_o))
    # 패널: 위 규칙(부모)으로 정해진 사이트면 어느 규칙인지 보여 줌
    storage(perms={"localhost": "eye"})
    page.goto(WEB.replace("127.0.0.1", "app.localhost") + "page2.html")
    page.bring_to_front()
    wait_until(lambda: "localhost 규칙" in panel.inner_text("#levelHint"), 5)
    check("패널: 하위 주소는 '(localhost 규칙)'으로 표시", "localhost 규칙" in panel.inner_text("#levelHint")
          and panel.get_attribute("#levels [data-level=eye]", "aria-checked") == "true", panel.inner_text("#levelHint"))
    storage(perms={})
    check("JS 오류 없음 (설정 화면)", not oerr, oerr[:3])
    opt.close()

    # ------------------------------------------------------------ 승인 · 사람에게 넘기기 (패널에서 답함)
    panel.click("#tabChat")
    page.goto(SONNUN + "/playground/")
    page.bring_to_front()
    time.sleep(0.6)

    def in_bg(fn):
        box: dict = {}
        def run():
            try:
                box["ok"] = fn()
            except SonnunError as exc:
                box["err"] = exc
        th_ = threading.Thread(target=run)
        th_.start()
        return th_, box

    def answer_card(button: str) -> str:
        wait_until(lambda: panel.locator(".req").count() > 0, 10)
        text = panel.inner_text("#requests")
        panel.click(f".req button:has-text('{button}')")
        return text

    s18 = b.find(text="S18")["elements"][0]["ref"]
    th_, box = in_bg(lambda: b.click(ref=s18))
    card = answer_card("허용")
    th_.join(20)
    badge_after = sw.evaluate("() => chrome.action.getBadgeText({})")
    check("승인: '결제' 버튼은 패널 카드가 뜨고, 허용하면 눌림", "결제" in card and "ok" in box
          and "S18 결제 버튼 (승인 필요)\n성공" in page.inner_text("#board"), (card, box))
    check("승인: 답한 뒤 아이콘 '!' 표시 해제", badge_after == "", badge_after)
    th_, box = in_bg(lambda: b.click(ref=s18))
    wait_until(lambda: panel.locator(".req").count() > 0, 10)
    check("승인 대기 중: 툴바 아이콘에 '!'", sw.evaluate("() => chrome.action.getBadgeText({})") == "!")
    panel.screenshot(path=str(SHOTS / "panel_approve_380.png"))
    answer_card("거부")
    th_.join(20)
    check("승인: 거부하면 denied 오류", isinstance(box.get("err"), SonnunError) and box["err"].code == "denied", box)
    storage(approvalTimeoutSec=2)
    t0 = time.time()
    try:
        b.click(ref=s18)
        check("승인: 답이 없으면 시간 초과로 하지 않음", False)
    except SonnunError as e:
        check("승인: 답이 없으면 시간 초과로 하지 않음", e.code == "denied" and "기다림" in str(e) and time.time() - t0 < 10, e)
    wait_until(lambda: panel.locator(".req").count() == 0, 5)
    check("승인: 시간 초과 뒤 카드 사라짐", panel.locator(".req").count() == 0)
    storage(approvalTimeoutSec=120, confirmOn=False)
    b.click(ref=s18)
    check("승인 끄면(설정) 바로 눌림", panel.locator(".req").count() == 0)
    storage(confirmOn=True)
    s19 = b.find(text="S19")["elements"][0]["ref"]
    th_, box = in_bg(lambda: b.click(ref=s19, accept_dialog=True))
    card = answer_card("허용")
    th_.join(20)
    check("확인창을 '확인'으로 답하려면 승인 필요 → 허용하면 '확인'", "확인창" in card and "ok" in box
          and any(d["answer"] == "확인" for d in box["ok"].get("dialogs") or []), (card, box.get("ok", {}).get("dialogs")))
    th_, box = in_bg(lambda: b.ask_user(message="CAPTCHA 를 풀어 주세요", timeout_sec=30))
    card = answer_card("다 했어요")
    th_.join(20)
    check("ask_user: 패널에 요청이 뜨고 '다 했어요' → done", "CAPTCHA" in card and box.get("ok", {}).get("done") is True, (card, box))
    th_, box = in_bg(lambda: b.ask_user(message="로그인해 주세요", timeout_sec=30))
    answer_card("못 해요")
    th_.join(20)
    check("ask_user: '못 해요' → done=false", box.get("ok", {}).get("done") is False and not box["ok"].get("timeout"), box)
    check("JS 오류 없음 (승인·요청 카드)", not [e for e in errors if "ERR_CONNECTION_REFUSED" not in e], errors[:3])

    # ------------------------------------------------------------ 서버 재시작 / 꺼짐
    stop_server(srv, th)
    wait_until(lambda: panel.get_attribute("#conn", "data-state") == "down", 12)
    check("패널: 서버 꺼지면 '서버 꺼짐' + 보내기 잠김", panel.inner_text("#conn") == "서버 꺼짐" and panel.is_disabled("#send"))
    panel.screenshot(path=str(SHOTS / "panel_down_380.png"))
    try:
        b.get_page()
        check("import: 서버 꺼짐 → server_down", False)
    except SonnunError as e:
        check("import: 서버 꺼짐 → server_down", e.code == "server_down", e)
    srv, th = start_server()
    check("서버 재시작: 확장이 다시 붙음", wait_until(lambda: get("/api/status")["extension"]["connected"], 20))
    check("서버 재시작: 대화 유지", len(get("/api/chats")) == n_chats + 3, len(get("/api/chats")))   # 패널 대화 2 + 작업 탭 대화 1
    check("서버 재시작 후 도구 동작", b.get_page(tab_id=tab_id)["url"].startswith("http://127.0.0.1:"))
    # 서버를 일부러 끈 동안 Chrome 이 찍는 연결 거부 로그는 예상된 것 (화면은 '서버 꺼짐'으로 처리함)
    errors = [e for e in errors if "ERR_CONNECTION_REFUSED" not in e]
    check("JS 오류 없음 (패널)", not errors, errors[:3])
    ctx.close()

stop_server(srv, th)
web.shutdown()
print(f"\n{sum(results)}/{len(results)} PASS   스크린샷: {SHOTS}")
if all(results):
    shutil.rmtree(TMP / "profile", ignore_errors=True)
sys.exit(0 if all(results) else 1)
