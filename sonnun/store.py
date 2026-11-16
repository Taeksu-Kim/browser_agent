"""SQLite 한 파일 -- 대화·메시지·활동 기록. (사이트 권한·멈춤은 확장 저장소에 있다: 실제로 검사하는 곳이 확장이라서)"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

SCHEMA = """
create table if not exists chats (
  id text primary key, backend text not null, title text not null default '',
  created text not null, updated text not null,
  claude_session text not null, claude_started integer not null default 0
);
create table if not exists messages (
  id integer primary key autoincrement, chat_id text not null references chats(id),
  role text not null, text text not null, steps text not null default '[]', error text not null default '',
  ts text not null
);
create index if not exists messages_chat on messages(chat_id, id);
create table if not exists activity (
  id integer primary key autoincrement, ts text not null, chat_id text, source text not null,
  tool text not null, args text not null, ok integer not null, summary text not null, url text not null default ''
);
"""


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("pragma table_info(activity)")}
            if "detail" not in cols:      # 2026-11-16 추가: 스크립트로 내보내기에 쓸 요소 이름 등
                c.execute("alter table activity add column detail text not null default '{}'")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _write(self, sql: str, params: tuple) -> int:
        with self._lock, self._conn() as c:
            return c.execute(sql, params).lastrowid or 0

    # ---------------------------------------------------------------- 대화
    def create_chat(self, backend: str) -> dict:
        cid, t = uuid.uuid4().hex[:12], now()
        self._write("insert into chats(id, backend, created, updated, claude_session) values (?,?,?,?,?)",
                    (cid, backend, t, t, str(uuid.uuid4())))
        return self.get_chat(cid)  # type: ignore[return-value]

    def get_chat(self, cid: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("select * from chats where id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def list_chats(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("select * from chats order by updated desc limit ?", (limit,))]

    def touch(self, cid: str, *, title: Optional[str] = None) -> None:
        if title is not None:
            self._write("update chats set updated=?, title=case when title='' then ? else title end where id=?",
                        (now(), title, cid))
        else:
            self._write("update chats set updated=? where id=?", (now(), cid))

    def mark_claude_started(self, cid: str) -> None:
        self._write("update chats set claude_started=1 where id=?", (cid,))

    def add_message(self, cid: str, role: str, text: str, *, steps: Optional[list] = None, error: str = "") -> None:
        self._write("insert into messages(chat_id, role, text, steps, error, ts) values (?,?,?,?,?,?)",
                    (cid, role, text, json.dumps(steps or [], ensure_ascii=False), error, now()))

    def messages(self, cid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("select role, text, steps, error, ts from messages where chat_id=? order by id", (cid,))
            return [{**dict(r), "steps": json.loads(r["steps"])} for r in rows]

    # ---------------------------------------------------------------- 활동 기록
    def log(self, *, chat_id: Optional[str], source: str, tool: str, args: dict, ok: bool, summary: str, url: str,
            detail: Optional[dict] = None) -> None:
        self._write("insert into activity(ts, chat_id, source, tool, args, ok, summary, url, detail) values (?,?,?,?,?,?,?,?,?)",
                    (now(), chat_id, source, tool, json.dumps(args, ensure_ascii=False), int(ok), summary, url,
                     json.dumps(detail or {}, ensure_ascii=False)))

    def activity(self, limit: int = 100, chat_id: Optional[str] = None) -> list[dict]:
        q, p = "select * from activity", ()
        if chat_id:
            q, p = q + " where chat_id=?", (chat_id,)
        with self._conn() as c:
            rows = c.execute(q + " order by id desc limit ?", (*p, limit)).fetchall()
        return [{**dict(r), "ok": bool(r["ok"]), "args": json.loads(r["args"]), "detail": json.loads(r["detail"] or "{}")}
                for r in rows]
