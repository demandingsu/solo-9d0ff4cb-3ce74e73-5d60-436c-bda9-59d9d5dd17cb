"""附件归档：重新读取原公告及其更正公告详情，抓取同站附件并按 SHA-256 保存。

关键语义：
- 输入必须是已采集的“原公告编号”；
- 每轮都重新读取数据库中该原公告及其更正公告的详情页，因此源站新增/删除的
  附件链接会在成功重跑后反映到可见清单；
- 所有详情页与所有附件都成功读取后才在一个事务内整体替换该项目的可见清单；
- 任一详情或附件读取失败（网络错误、非 2xx、超过单文件大小上限等）时，
  数据库中的旧清单与旧内容文件完全不动；
- 内容按 SHA-256 寻址保存；同一链接内容变化会得到新的摘要和新的文件，
  下载接口始终返回当前可见清单对应的内容。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config, database


class ArchiveError(Exception):
    """附件归档失败（详情或附件不可读、超限、链接不在同站等）。"""


class ArchiveNotFound(Exception):
    """输入的原公告编号未经采集或不是原公告。"""


@dataclass(frozen=True)
class AttachmentLink:
    announcement_code: str
    url: str
    filename: str


@dataclass(frozen=True)
class StoredAttachment:
    sha256: str
    size: int


# ---------------------------------------------------------------------------
# 配置读取（运行时读取环境变量，便于测试/容器调整）
# ---------------------------------------------------------------------------
def _attachment_dir() -> Path:
    raw = os.getenv("TENDER_ATTACHMENT_DIR")
    root = Path(raw) if raw else Path(config.settings.attachment_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _max_attachment_bytes() -> int:
    raw = os.getenv("TENDER_ATTACHMENT_MAX_BYTES")
    if raw is not None:
        return int(raw)
    return int(config.settings.max_attachment_bytes)


def _limit_text(max_bytes: int) -> str:
    if max_bytes == 10 * 1024 * 1024:
        return "10 MiB（10485760 字节）"
    return f"{max_bytes} 字节"


# ---------------------------------------------------------------------------
# 详情页与链接解析
# ---------------------------------------------------------------------------
def _same_origin(url: str, base: str) -> bool:
    u, b = urlparse(url), urlparse(base)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc)


def _safe_filename(raw: str | None, url: str) -> str:
    name = " ".join((raw or "").split())
    name = name.replace("\x00", "")
    # 去掉路径分隔符与可在文件系统/响应头中造成问题的控制字符
    name = re.sub(r"[\\/]+", "_", name)
    name = "".join(ch for ch in name if ord(ch) >= 32).strip(" .")
    if not name:
        name = unquote(Path(urlparse(url).path).name)
        name = re.sub(r"[\\/]+", "_", name).strip(" .")
    if not name:
        name = "attachment"
    return name[:200]


def _fetch_detail(client: httpx.Client, code: str, url: str) -> str:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        raise ArchiveError(f"读取公告详情失败 {code}（{url}）: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise ArchiveError(
            f"读取公告详情失败 {code}（{url}）: HTTP {resp.status_code}"
        )
    return resp.text


# 常见附件扩展名，用于在缺少显式 class 时兜底识别（避免把返回列表等 .html 链接
# 当作附件）。
_ATTACHMENT_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".txt", ".csv", ".jpg", ".jpeg", ".png",
    ".gif", ".bmp", ".tif", ".tiff", ".dwg", ".dxf",
}


def _extract_links(
    html_text: str, detail_url: str, announcement_code: str
) -> list[AttachmentLink]:
    soup = BeautifulSoup(html_text, "html.parser")
    article = soup.select_one("article.announcement-detail")
    if article is None:
        raise ArchiveError(
            f"详情页 {announcement_code}（{detail_url}）缺少 "
            "article.announcement-detail"
        )

    # 显式标记优先；同时覆盖常见“附件容器”内的普通链接。之后再兜底收集
    # 页面中带常见附件后缀的普通链接，避免漏掉未加 class 的附件条目。
    anchors = list(
        article.select(
            "a.attachment-link[href], a[data-attachment][href], "
            ".attachment-list a[href], .attachments a[href]"
        )
    )
    explicit_ids = {id(a) for a in anchors}
    for a in article.select("a[href]"):
        if id(a) in explicit_ids:
            continue
        suffix = Path(urlparse(urljoin(detail_url, a["href"])).path).suffix.lower()
        if suffix in _ATTACHMENT_SUFFIXES:
            anchors.append(a)

    links: list[AttachmentLink] = []
    seen: set[str] = set()
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href:
            continue
        url = urljoin(detail_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ArchiveError(
                f"公告 {announcement_code} 的附件链接协议不受支持: {url}"
            )
        # “逐份从同站下载”：只允许与详情页同源的附件地址，避免跨站抓取。
        if not _same_origin(url, detail_url):
            raise ArchiveError(
                f"公告 {announcement_code} 的附件链接不在同站: {url}"
            )
        if url in seen:
            continue
        seen.add(url)
        filename = _safe_filename(
            a.get("data-filename") or a.get_text(" ", strip=True), url
        )
        links.append(
            AttachmentLink(
                announcement_code=announcement_code,
                url=url,
                filename=filename,
            )
        )
    return links


# ---------------------------------------------------------------------------
# 下载与内容寻址存储
# ---------------------------------------------------------------------------
def _download_attachment(
    client: httpx.Client,
    url: str,
    max_bytes: int,
    root: Path,
) -> StoredAttachment:
    tmp_dir = root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with client.stream("GET", url) as resp:
            if resp.status_code // 100 != 2:
                raise ArchiveError(
                    f"读取附件失败 {url}: HTTP {resp.status_code}"
                )

            declared = resp.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > max_bytes:
                        raise ArchiveError(
                            "附件超过单文件上限 "
                            f"{_limit_text(max_bytes)}: {url}"
                        )
                except ValueError:
                    pass

            fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix="attachment-")
            tmp_path = Path(tmp_name)
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as fh:
                    for chunk in resp.iter_bytes():
                        if size + len(chunk) > max_bytes:
                            raise ArchiveError(
                                "附件超过单文件上限 "
                                f"{_limit_text(max_bytes)}: {url}"
                            )
                        size += len(chunk)
                        digest.update(chunk)
                        fh.write(chunk)

                sha256 = digest.hexdigest()
                target = root / sha256
                if target.exists():
                    # 内容寻址：摘要相同意味着内容相同，无需重复写盘。
                    tmp_path.unlink()
                else:
                    os.replace(tmp_path, target)
                    os.chmod(target, 0o644)
                return StoredAttachment(sha256=sha256, size=size)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
    except ArchiveError:
        raise
    except httpx.HTTPError as exc:
        raise ArchiveError(f"读取附件失败 {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def _announcement_group(
    conn, original_code: str
) -> tuple[Any, list[Any]]:
    original = database.fetch_announcement(conn, original_code)
    if original is None or original["kind"] != "original":
        raise ArchiveNotFound(
            f"未找到已采集的原公告编号: {original_code}"
        )
    corrections = database.fetch_corrections(conn, original_code)
    return original, corrections


def _download_round(original_code: str) -> list[dict[str, Any]]:
    """读取整个公告组的详情并下载全部附件；任一步失败抛出 ArchiveError。"""
    with database.get_conn() as conn:
        original, corrections = _announcement_group(conn, original_code)

    # 原公告在前，更正公告按发布时间/编号排序；仅影响清单展示顺序。
    announcements = [original, *corrections]

    root = _attachment_dir()
    max_bytes = _max_attachment_bytes()
    links_by_code: dict[str, list[AttachmentLink]] = {}
    plan_order: list[str] = []

    headers = {"User-Agent": config.settings.user_agent}
    with httpx.Client(
        timeout=config.settings.request_timeout,
        # 不自动跟随重定向：跨站重定向不应绕过同源限制，也避免下载到非预期内容。
        follow_redirects=False,
        headers=headers,
    ) as client:
        for ann in announcements:
            html_text = _fetch_detail(client, ann["code"], ann["detail_url"])
            links = _extract_links(html_text, ann["detail_url"], ann["code"])
            links_by_code[ann["code"]] = links
            for link in links:
                if link.url not in plan_order:
                    plan_order.append(link.url)

        stored: dict[str, StoredAttachment] = {}
        for url in plan_order:
            stored[url] = _download_attachment(client, url, max_bytes, root)

    archived_at = database.utcnow_iso()
    rows: list[dict[str, Any]] = []
    for ann in announcements:
        for link in links_by_code[ann["code"]]:
            blob = stored[link.url]
            rows.append(
                {
                    "original_code": original_code,
                    "announcement_code": ann["code"],
                    "url": link.url,
                    "filename": link.filename,
                    "sha256": blob.sha256,
                    "size": blob.size,
                    "archived_at": archived_at,
                }
            )
    return rows


def archive_attachments(original_code: str) -> dict[str, Any]:
    """归档指定原公告及其更正公告的全部同站附件。"""
    original_code = original_code.strip()
    started_at = database.utcnow_iso()

    try:
        rows = _download_round(original_code)
    except ArchiveNotFound:
        raise
    except ArchiveError as exc:
        # 失败：不触碰 attachments 表，旧清单与旧文件保持完整可下载。
        with database.get_conn() as conn:
            database.record_attachment_run(
                conn,
                original_code=original_code,
                status="failed",
                items_archived=0,
                error=str(exc),
                started_at=started_at,
            )
            conn.commit()
        raise

    # 成功：单事务整体替换清单；提交前对外仍只有旧清单。
    with database.get_conn() as conn:
        try:
            database.replace_attachments(conn, original_code, rows)
            database.record_attachment_run(
                conn,
                original_code=original_code,
                status="success",
                items_archived=len(rows),
                error=None,
                started_at=started_at,
            )
            conn.commit()
        except Exception as exc:  # pragma: no cover - 数据库异常兜底
            conn.rollback()
            raise ArchiveError(f"写入附件清单失败: {exc}") from exc

    return get_manifest(original_code)


def get_manifest(original_code: str) -> dict[str, Any]:
    """返回当前对外可见的附件清单（从未归档过则为空清单）。"""
    original_code = original_code.strip()
    with database.get_conn() as conn:
        original, _ = _announcement_group(conn, original_code)
        rows = database.fetch_attachments(conn, original_code)
        last_run = database.fetch_last_successful_attachment_run(
            conn, original_code
        )

    archived_at = None
    if last_run is not None:
        archived_at = last_run["finished_at"]
    elif rows:
        archived_at = rows[0]["archived_at"]

    attachments = [
        {
            "id": row["id"],
            "announcement_code": row["announcement_code"],
            "filename": row["filename"],
            "url": row["url"],
            "original_url": row["url"],
            "source_url": row["url"],
            "sha256": row["sha256"],
            "digest": row["sha256"],
            "size": row["size"],
            "download_url": f"/api/attachments/{row['id']}/download",
        }
        for row in rows
    ]
    return {
        "original_code": original_code,
        "project_name": original["project_name"],
        "archived_at": archived_at,
        "count": len(attachments),
        "archived": len(attachments),
        "attachments": attachments,
    }


def resolve_attachment(attachment_id: int) -> tuple[Path, str, str]:
    """返回 (内容文件路径, 下载文件名, SHA-256)；不存在时抛出 ArchiveNotFound。"""
    with database.get_conn() as conn:
        row = database.fetch_attachment(conn, attachment_id)
    if row is None:
        raise ArchiveNotFound(f"附件不存在: {attachment_id}")
    root = _attachment_dir()
    path = root / row["sha256"]
    if not path.is_file():
        # 清单存在但内容文件丢失属于存储异常，不应悄悄返回旧内容。
        raise ArchiveNotFound(f"附件内容文件缺失: {attachment_id}")
    return path, row["filename"], row["sha256"]
