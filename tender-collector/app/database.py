"""SQLite 持久化层。

announcements 以公告编号 (code) 为唯一键：重复采集走 UPSERT，不会产生重复记录。
collection_runs 记录每次采集的成败，失败时不触碰 announcements，
因此上次成功采集的完整结果始终保留。

附件归档同样采用“先全部下载成功、再单事务整体替换”的策略：
attachments 中某个原公告编号下的行构成对外可见的附件清单，
替换事务提交前旧行与旧文件始终可用。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    project_name    TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    deadline        TEXT,
    corrects_code   TEXT,
    detail_url      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_project
    ON announcements(project_name);
CREATE INDEX IF NOT EXISTS idx_announcements_corrects
    ON announcements(corrects_code);

CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    items_collected INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);

-- 每轮附件归档的运行记录（成功/失败），便于排查。
CREATE TABLE IF NOT EXISTS attachment_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    original_code   TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    items_archived  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachment_runs_code
    ON attachment_runs(original_code, id);

-- 当前对外可见的附件清单。成功归档时在同一事务内按 original_code 整体替换。
-- sha256 同时是内容寻址存储中的文件名；同一链接内容变化会得到新的 sha256，
-- 因此旧版本仍可下载直到替换提交，新版本不会与旧内容混淆。
CREATE TABLE IF NOT EXISTS attachments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    original_code       TEXT NOT NULL,
    announcement_code   TEXT NOT NULL,
    url                 TEXT NOT NULL,
    filename            TEXT NOT NULL,
    sha256              TEXT NOT NULL,
    size                INTEGER NOT NULL,
    archived_at         TEXT NOT NULL,
    UNIQUE (original_code, announcement_code, url)
);
CREATE INDEX IF NOT EXISTS idx_attachments_original
    ON attachments(original_code, id);
CREATE INDEX IF NOT EXISTS idx_attachments_digest
    ON attachments(sha256);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_announcements(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """在调用方给定的事务内 UPSERT；重复 code 不产生重复记录。"""
    now = utcnow_iso()
    # 原公告先写，保证更正公告引用的数据已存在（顺序不影响业务判定，仅约束友好）
    rows = sorted(rows, key=lambda r: 0 if r["kind"] == "original" else 1)
    conn.executemany(
        """
        INSERT INTO announcements
            (code, title, kind, project_name, published_at, deadline,
             corrects_code, detail_url, last_seen_at)
        VALUES
            (:code, :title, :kind, :project_name, :published_at, :deadline,
             :corrects_code, :detail_url, :last_seen_at)
        ON CONFLICT(code) DO UPDATE SET
            title=excluded.title,
            kind=excluded.kind,
            project_name=excluded.project_name,
            published_at=excluded.published_at,
            deadline=excluded.deadline,
            corrects_code=excluded.corrects_code,
            detail_url=excluded.detail_url,
            last_seen_at=excluded.last_seen_at
        """,
        [{**r, "last_seen_at": now} for r in rows],
    )


def record_run(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    status: str,
    items_collected: int,
    error: str | None,
    started_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO collection_runs
            (source_url, status, items_collected, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_url, status, items_collected, error, started_at, utcnow_iso()),
    )


def fetch_all_announcements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM announcements ORDER BY project_name, published_at, code"
        )
    )


def fetch_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )


# ---------------------------------------------------------------------------
# 附件归档
# ---------------------------------------------------------------------------
def fetch_announcement(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM announcements WHERE code = ?", (code,)
    ).fetchone()


def fetch_corrections(
    conn: sqlite3.Connection, original_code: str
) -> list[sqlite3.Row]:
    """返回引用了指定原公告的已采集更正公告（顺序确定）。"""
    return list(
        conn.execute(
            """
            SELECT * FROM announcements
            WHERE kind = 'correction' AND corrects_code = ?
            ORDER BY published_at, code
            """,
            (original_code,),
        )
    )


def replace_attachments(
    conn: sqlite3.Connection,
    original_code: str,
    rows: list[dict[str, Any]],
) -> None:
    """在调用方事务内整体替换某个项目的可见附件清单。

    以 (announcement_code, url) 为稳定身份：
    - 同一链接内容变化时原地更新摘要，附件 id 与下载地址保持不变，
      下载会返回新内容；
    - 源站已删除的链接删除对应行；
    - 整个操作在一个事务内完成，配合调用方 commit/rollback 实现
      “全部成功才切换”。提交前旧行与旧内容始终可用。
    """
    existing = {
        (r["announcement_code"], r["url"]): r
        for r in conn.execute(
            """
            SELECT id, announcement_code, url FROM attachments
            WHERE original_code = ?
            """,
            (original_code,),
        )
    }

    kept: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["announcement_code"], row["url"])
        if key in kept:
            continue
        kept.add(key)
        current = existing.get(key)
        if current is None:
            conn.execute(
                """
                INSERT INTO attachments
                    (original_code, announcement_code, url, filename, sha256,
                     size, archived_at)
                VALUES
                    (:original_code, :announcement_code, :url, :filename,
                     :sha256, :size, :archived_at)
                """,
                row,
            )
        else:
            # 保留 id，确保同一链接的下载地址稳定；只更新内容相关字段。
            conn.execute(
                """
                UPDATE attachments
                SET filename = :filename, sha256 = :sha256, size = :size,
                    archived_at = :archived_at
                WHERE id = :id
                """,
                {**row, "id": current["id"]},
            )

    for key, current in existing.items():
        if key not in kept:
            conn.execute(
                "DELETE FROM attachments WHERE id = ?", (current["id"],)
            )


def fetch_attachments(
    conn: sqlite3.Connection, original_code: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM attachments
            WHERE original_code = ?
            ORDER BY id
            """,
            (original_code,),
        )
    )


def fetch_attachment(
    conn: sqlite3.Connection, attachment_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()


def record_attachment_run(
    conn: sqlite3.Connection,
    *,
    original_code: str,
    status: str,
    items_archived: int,
    error: str | None,
    started_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO attachment_runs
            (original_code, status, items_archived, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            original_code,
            status,
            items_archived,
            error,
            started_at,
            utcnow_iso(),
        ),
    )


def fetch_attachment_runs(
    conn: sqlite3.Connection,
    original_code: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    if original_code:
        return list(
            conn.execute(
                """
                SELECT * FROM attachment_runs
                WHERE original_code = ?
                ORDER BY id DESC LIMIT ?
                """,
                (original_code, limit),
            )
        )
    return list(
        conn.execute(
            "SELECT * FROM attachment_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )


def fetch_last_successful_attachment_run(
    conn: sqlite3.Connection, original_code: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM attachment_runs
        WHERE original_code = ? AND status = 'success'
        ORDER BY id DESC LIMIT 1
        """,
        (original_code,),
    ).fetchone()
