"""python -m sonnun serve [--port 8771] [--data data/sonnun.db]
python -m sonnun run "이 페이지 요약해줘" [--backend local|claude] [--url http://127.0.0.1:8771]
python -m sonnun demo [--slow 1.2] [--approve] [--url http://127.0.0.1:8771]   # 플레이그라운드 시나리오를 내 Chrome 에서 차례로
python -m sonnun export [대화 id] [--format sonnun|playwright] [-o 파일]      # 대화에서 성공한 조작을 스크립트로

run 은 떠 있는 서버에 대화를 하나 만들어 보내고 진행을 터미널에 찍는다 (사이드 패널 대화 목록에도 보인다).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def serve(a: argparse.Namespace) -> None:
    import uvicorn

    from sonnun.app import create_app
    url = f"http://127.0.0.1:{a.port}"
    print(f"손눈서브 서버 {url} (Chrome 확장이 여기에 붙습니다)")
    uvicorn.run(create_app(Path(a.data), server_url=url), host="127.0.0.1", port=a.port, log_level="warning")


def _post(url: str, body: dict) -> urllib.request.Request:
    return urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")


def run(a: argparse.Namespace) -> int:
    base = a.url.rstrip("/")
    try:
        with urllib.request.urlopen(_post(f"{base}/api/chats", {"backend": a.backend}), timeout=10) as r:
            chat = json.load(r)
        resp = urllib.request.urlopen(_post(f"{base}/api/chats/{chat['id']}/messages", {"text": a.text}), timeout=900)
    except urllib.error.HTTPError as exc:
        print(f"실패: {exc.code} {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"서버에 연결할 수 없음 ({base}) — python -m sonnun serve: {exc}", file=sys.stderr)
        return 1
    code = 0
    with resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev["type"] == "tool":
                print(f"  → {ev['name']} {json.dumps(ev['input'], ensure_ascii=False)}")
            elif ev["type"] == "tool_result":
                print(f"    {'✓' if ev['ok'] else '✗'} {ev['summary']}")
            elif ev["type"] == "done":
                print(f"\n{ev.get('text', '')}")
            elif ev["type"] == "error":
                print(f"\n오류 ({ev['kind']}): {ev['message']}", file=sys.stderr)
                code = 1
    return code


def demo(a: argparse.Namespace) -> int:
    from sonnun.client import Sonnun, SonnunError
    from sonnun.demo import print_rows, run as run_demo
    print(f"플레이그라운드를 Chrome 지금 탭에 엽니다: {a.url.rstrip('/')}/playground/")
    try:
        rows = run_demo(Sonnun(a.url), a.url, slow=a.slow, approve=a.approve)
    except SonnunError as exc:
        print(f"실패 ({exc.code}): {exc}", file=sys.stderr)
        return 1
    print_rows(rows)
    return 0 if all(r["예상대로"] for r in rows) else 1


def export(a: argparse.Namespace) -> int:
    base = a.url.rstrip("/")
    try:
        cid = a.chat
        if not cid:
            with urllib.request.urlopen(f"{base}/api/chats", timeout=10) as r:
                chats = json.load(r)
            if not chats:
                print("대화가 없음", file=sys.stderr)
                return 1
            cid = chats[0]["id"]
        with urllib.request.urlopen(f"{base}/api/chats/{cid}/script?format={a.format}", timeout=10) as r:
            text = r.read().decode()
    except urllib.error.HTTPError as exc:
        print(f"실패: {exc.code} {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"서버에 연결할 수 없음 ({base}) — python -m sonnun serve: {exc}", file=sys.stderr)
        return 1
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"썼음: {a.out} (대화 {cid}, 형식 {a.format})")
    else:
        sys.stdout.write(text)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="sonnun")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8771)
    s.add_argument("--data", default=str(ROOT / "data" / "sonnun.db"))
    r = sub.add_parser("run")
    r.add_argument("text")
    r.add_argument("--backend", choices=["local", "claude"], default="local")
    r.add_argument("--url", default="http://127.0.0.1:8771")
    d = sub.add_parser("demo")
    d.add_argument("--slow", type=float, default=1.2, help="시나리오 사이 대기(초) — 눈으로 따라갈 수 있게")
    d.add_argument("--url", default="http://127.0.0.1:8771")
    d.add_argument("--approve", action="store_true", help="S18 결제 버튼도 실행 (패널에 뜬 승인 카드에서 '허용'을 눌러야 함)")
    e = sub.add_parser("export", help="대화에서 성공한 조작을 Python 스크립트로")
    e.add_argument("chat", nargs="?", default="", help="대화 id (없으면 가장 최근 대화)")
    e.add_argument("--format", choices=["sonnun", "playwright"], default="sonnun")
    e.add_argument("-o", "--out", default="")
    e.add_argument("--url", default="http://127.0.0.1:8771")
    a = ap.parse_args()
    if a.cmd == "export":
        sys.exit(export(a))
    if a.cmd == "serve":
        serve(a)
    elif a.cmd == "demo":
        sys.exit(demo(a))
    else:
        sys.exit(run(a))


if __name__ == "__main__":
    main()
