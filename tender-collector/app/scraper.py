"""公告站采集与 HTML 解析。

流程：
1. 从用户提交的列表页开始，沿分页链接抓取全部列表页（去重、防环、限量）；
2. 收集列表页中全部详情页链接（同源、去重）；
3. 逐页抓取详情并解析为结构化公告。

任一列表页或详情页读取失败（网络错误、非 2xx、HTML 缺关键字段）都抛出
CollectionError，由上层整体放弃本次采集，数据库保留上次成功结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings


class CollectionError(Exception):
    """采集过程中任一环节失败。"""


@dataclass
class ParsedAnnouncement:
    code: str
    title: str
    kind: str
    project_name: str
    published_at: str
    deadline: str | None
    corrects_code: str | None
    detail_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "kind": self.kind,
            "project_name": self.project_name,
            "published_at": self.published_at,
            "deadline": self.deadline,
            "corrects_code": self.corrects_code,
            "detail_url": self.detail_url,
        }


def _same_origin(url: str, base: str) -> bool:
    u, b = urlparse(url), urlparse(base)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc)


def _get(client: httpx.Client, url: str) -> str:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        raise CollectionError(f"读取页面失败 {url}: {exc}") from exc
    if resp.status_code // 100 != 2:
        raise CollectionError(f"读取页面失败 {url}: HTTP {resp.status_code}")
    return resp.text


def _parse_list(html_text: str, list_url: str) -> tuple[set[str], set[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    detail_urls: set[str] = set()
    page_urls: set[str] = set()

    for a in soup.select("ul.announcement-list a.detail-link[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            detail_urls.add(href)

    for a in soup.select("nav.pagination a[href]"):
        href = urljoin(list_url, a["href"])
        if _same_origin(href, list_url):
            page_urls.add(href)

    if not detail_urls:
        raise CollectionError(f"列表页 {list_url} 未解析到任何详情链接")
    return detail_urls, page_urls


def _field(soup: BeautifulSoup, label: str) -> str | None:
    cell = soup.select_one(f'th[data-field="{label}"] + td')
    if cell is None:
        return None
    text = cell.get_text(strip=True)
    return text or None


def _parse_detail(html_text: str, detail_url: str) -> ParsedAnnouncement:
    soup = BeautifulSoup(html_text, "html.parser")
    article = soup.select_one("article.announcement-detail")
    if article is None:
        raise CollectionError(f"详情页 {detail_url} 缺少 article.announcement-detail")

    code = article.get("data-code") or _field(soup, "公告编号")
    kind_raw = article.get("data-kind")
    published = article.get("data-published-at") or _field(soup, "发布时间")
    corrects = article.get("data-corrects") or _field(soup, "原公告编号")

    title_el = soup.select_one("h1.detail-title")
    title = title_el.get_text(strip=True) if title_el else None
    project_name = _field(soup, "项目名称")
    deadline = _field(soup, "投标截止时间")

    kind = {"original": "original", "correction": "correction"}.get(kind_raw or "", "")
    if not kind:
        label = _field(soup, "公告类型") or ""
        kind = "correction" if "更正" in label else "original"

    missing = [
        name
        for name, val in (
            ("公告编号", code),
            ("公告标题", title),
            ("项目名称", project_name),
            ("发布时间", published),
        )
        if not val
    ]
    if missing:
        raise CollectionError(f"详情页 {detail_url} 缺少必填字段: {', '.join(missing)}")
    if kind == "correction" and not corrects:
        raise CollectionError(f"更正公告 {code}（{detail_url}）缺少原公告编号")

    return ParsedAnnouncement(
        code=code.strip(),
        title=title.strip(),
        kind=kind,
        project_name=project_name.strip(),
        published_at=published.strip(),
        deadline=deadline.strip() if deadline else None,
        corrects_code=corrects.strip() if corrects else None,
        detail_url=detail_url,
    )


def collect(list_url: str) -> list[dict[str, Any]]:
    """抓取整个公告站并返回结构化公告列表（顺序不保证，归并层不依赖顺序）。"""
    headers = {"User-Agent": settings.user_agent}
    with httpx.Client(
        timeout=settings.request_timeout, follow_redirects=True, headers=headers
    ) as client:
        # 1) 遍历全部列表页
        seen_pages: set[str] = set()
        queue: list[str] = [list_url]
        detail_urls: set[str] = set()

        while queue:
            if len(seen_pages) >= settings.max_list_pages:
                raise CollectionError(
                    f"列表页数量超过上限 {settings.max_list_pages}，疑似分页环"
                )
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)

            html_text = _get(client, page_url)
            details, more_pages = _parse_list(html_text, page_url)
            detail_urls.update(details)
            for u in more_pages:
                if u not in seen_pages and u not in queue:
                    queue.append(u)

        # 2) 逐页抓取详情 —— 任一失败即整体失败
        announcements: list[dict[str, Any]] = []
        for detail_url in sorted(detail_urls):  # 排序仅为日志/测试的确定性
            html_text = _get(client, detail_url)
            announcements.append(_parse_detail(html_text, detail_url).as_dict())

    return announcements
