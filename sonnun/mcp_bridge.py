"""Claude Code 세션용 MCP 서버 (stdio) -- 손눈서브 도구를 Claude 에게 보여 주고, 실행은 떠 있는 sonnun 서버
(POST /api/tools/{name}) 에 넘긴다. 그래서 활동 기록·중지·권한이 다른 창구와 같은 곳에서 처리된다.

    claude -p ... --mcp-config '{"mcpServers": {"sonnun": {"command": "<python>",
        "args": ["-m", "sonnun.mcp_bridge", "--url", "http://127.0.0.1:8771", "--chat", "<chat id>"]}}}'

다른 MCP 클라이언트(로컬 모델을 붙인 Claude Code 등)에서도 --chat 없이 그대로 쓸 수 있다.
MCP stdio = 줄마다 JSON-RPC 2.0. 쓰는 메서드만 손으로 구현 (meal_log/mcp_bridge.py 와 같은 방식, mcp 패키지 없음).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from sonnun.tools import TOOLS, render_for_model, summarize

PROTOCOL = "2025-06-18"


def _call(url: str, chat: str, name: str, args: dict) -> tuple[str, bool]:
    headers = {"Content-Type": "application/json", "X-Sonnun-Source": "claude" if chat else "api"}
    if chat:
        headers["X-Sonnun-Chat"] = chat
    req = urllib.request.Request(f"{url}/api/tools/{name}", data=json.dumps(args, ensure_ascii=False).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=660) as r:   # 사용자 승인·ask_user 를 기다릴 수 있다
            result = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return f"오류: {exc.read().decode(errors='replace')[:300]}", True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"오류: 손눈서브 서버에 연결할 수 없음 ({url}): {exc}", True
    # 첫 줄 요약은 화면의 단계 표시용 (chat.ClaudeBackend 가 읽는다), 나머지는 모델이 볼 결과
    return f"요약: {summarize(name, args, result)}\n{render_for_model(name, result)}", "error" in result


def handle(msg: dict, url: str, chat: str) -> dict | None:
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:                       # 알림(notifications/*)에는 답하지 않는다
        return None
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "sonnun", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in TOOLS.values()]}
    elif method == "tools/call":
        p = msg.get("params") or {}
        text, is_error = _call(url, chat, str(p.get("name")), p.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8771")
    ap.add_argument("--chat", default="")
    a = ap.parse_args(argv)
    url = a.url.rstrip("/")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            out = handle(json.loads(line), url, a.chat)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
