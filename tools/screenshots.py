"""README 캡처 만들기 — 실제 확장·서버·로컬 모델로 찍는다 (꾸민 답 없음).

    python tools/screenshots.py            # docs/images/*.png

필요: 로컬 LLM(vLLM, 이미지 입력 켬 — look 용)이 켜져 있어야 대화 캡처가 진짜 답으로 나온다.
Chrome 의 사이드 패널은 자동화로 열 수 없어서, 패널 화면을 같은 크기 창으로 띄워 페이지 옆에 붙여 그린다.
사이트 권한 예시는 example 도메인만 쓴다.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from sonnun.app import create_app  # noqa: E402
from sonnun.client import Sonnun  # noqa: E402
from sonnun.demo import run as demo_run  # noqa: E402

OUT = ROOT / "docs" / "images"
TMP = Path(tempfile.mkdtemp(prefix="sonnun_shots_"))
EXT = str(ROOT / "extension")
PAGE_W, PANEL_W, H = 960, 400, 800


def side_by_side(page_png: Path, panel_png: Path, out: Path) -> None:
    """페이지 + 오른쪽 사이드 패널 (Chrome 에 붙은 모습처럼)."""
    a, b = Image.open(page_png), Image.open(panel_png)
    img = Image.new("RGB", (a.width + b.width + 1, max(a.height, b.height)), "#dfe2e6")
    img.paste(a, (0, 0))
    img.paste(b, (a.width + 1, 0))
    ImageDraw.Draw(img).line([(a.width, 0), (a.width, img.height)], fill="#c4c9d0", width=1)
    img.save(out, optimize=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = f"http://127.0.0.1:{port}"
    app = create_app(TMP / "s.db", server_url=server)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(1.5)
    b = Sonnun(server)
    fallback = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    with sync_playwright() as p:
        kw = dict(headless=True, viewport={"width": PAGE_W, "height": H},
                  args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        try:
            ctx = p.chromium.launch_persistent_context(str(TMP / "profile"), channel="chromium", **kw)
        except Exception:  # noqa: BLE001
            ctx = p.chromium.launch_persistent_context(str(TMP / "profile"), executable_path=str(fallback[-1]), **kw)
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        ext_id = sw.url.split("/")[2]
        sw.evaluate("u => chrome.storage.local.set({server: u})", server)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(server + "/playground/")
        for _ in range(50):
            if b.status()["extension"]["connected"]:
                break
            time.sleep(0.2)
        win = sw.evaluate("async () => (await chrome.windows.getLastFocused({windowTypes: ['normal']})).id")
        with ctx.expect_page() as info:
            sw.evaluate("([u]) => chrome.windows.create({url: u, type: 'popup', width: 420, height: 860})",
                        [f"chrome-extension://{ext_id}/sidepanel.html?window={win}"])
        panel = info.value
        panel.set_viewport_size({"width": PANEL_W, "height": H})
        panel.wait_for_load_state()
        time.sleep(1.5)

        # ① 대화: 실제 로컬 Qwen + look(비전)
        page.bring_to_front()
        page.evaluate("document.querySelector('#s17').scrollIntoView({block: 'center'})")
        panel.fill("#input", "S17 캔버스에 뭐가 그려져 있는지 설명해줘")
        panel.press("#input", "Enter")
        for _ in range(300):
            if panel.is_visible("#send") and panel.locator(".msg.assistant .md").count():
                break
            time.sleep(0.5)
        time.sleep(0.5)
        page.evaluate("document.querySelector('#s17').scrollIntoView({block: 'center'})")
        page.screenshot(path=str(TMP / "page1.png"))
        panel.screenshot(path=str(TMP / "panel1.png"))
        side_by_side(TMP / "page1.png", TMP / "panel1.png", OUT / "chat.png")
        print("답:", panel.locator(".msg.assistant .md").last.inner_text()[:200])

        # ② 승인 카드: 에이전트가 '결제하기'를 누르려 함 → 패널에서 허용/거부
        ref = b.find(text="S18")["elements"][0]["ref"]
        th = threading.Thread(target=lambda: _quiet(lambda: b.click(ref=ref)))
        th.start()
        for _ in range(50):
            if panel.locator(".req").count():
                break
            time.sleep(0.2)
        page.evaluate("document.querySelector('#s18').scrollIntoView({block: 'center'})")
        time.sleep(0.3)
        page.screenshot(path=str(TMP / "page2.png"))
        panel.screenshot(path=str(TMP / "panel2.png"))
        side_by_side(TMP / "page2.png", TMP / "panel2.png", OUT / "approve.png")
        panel.click(".req button:has-text('거부')")
        th.join(10)

        # ③ 권한 단계 설명 (ⓘ)
        panel.click("#levelInfo")
        time.sleep(0.2)
        panel.screenshot(path=str(OUT / "panel-levels.png"))
        panel.click("#levelInfo")

        # ④ 설정 화면: 사이트 권한 목록 (example 도메인)
        sw.evaluate("""() => chrome.storage.local.set({perms: {
            'bank.example': 'off', 'stock.example': 'off', 'mail.example.com': 'eye',
            'intranet.example.com': 'eye', 'approval.intranet.example.com': 'off', 'docs.example.org': 'hand'}})""")
        opt = ctx.new_page()
        opt.set_viewport_size({"width": 900, "height": 1000})
        opt.goto(f"chrome-extension://{ext_id}/options.html")
        time.sleep(1.0)
        opt.screenshot(path=str(OUT / "settings.png"))
        opt.close()
        sw.evaluate("() => chrome.storage.local.set({perms: {}})")

        # ⑤ 플레이그라운드: 시나리오를 차례로 실행한 뒤 점수판
        page.bring_to_front()
        demo_run(b, server, slow=0.3, out=lambda _m: None)
        page.goto(server + "/playground/") if "playground" not in page.evaluate("location.href") else None
        page.bring_to_front()
        page.set_viewport_size({"width": 1100, "height": 760})
        page.evaluate("scrollTo(0, 0)")
        time.sleep(0.5)
        page.screenshot(path=str(OUT / "playground.png"))
        ctx.close()
    srv.should_exit = True
    for f in sorted(OUT.glob("*.png")):
        print(f.relative_to(ROOT), Image.open(f).size)
    return 0


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 -- 거부되면 오류가 정상
        pass


if __name__ == "__main__":
    sys.exit(main())
