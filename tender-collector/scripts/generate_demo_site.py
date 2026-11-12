#!/usr/bin/env python3
"""生成一个本地分页 HTML 示例公告站（市政工程招投标公告）。

生成结果写入 demo_site/，纯静态文件，可用任意 HTTP 服务器托管：
    python3 -m http.server 8001 --directory demo_site

数据特意构造为：
- 列表页按发布时间倒序排列（更正公告排在其引用的原公告之前），
  服务端不能依赖页面顺序来判断截止时间；
- 每个项目的“生效截止时间”由发布时间最新的那份公告（原公告或更正公告）决定；
- 含已过期与未过期两种项目。
"""
from __future__ import annotations

import argparse
import html
from dataclasses import dataclass, field
from pathlib import Path

PAGE_SIZE = 4
OUT_DIR = Path(__file__).resolve().parent.parent / "demo_site"
ATTACHMENT_DIR = OUT_DIR / "attachments"


@dataclass
class Attachment:
    path: str                      # 相对 demo_site/attachments/ 的文件名
    name: str                      # 展示名 / 下载文件名
    # None 表示源站上的文件不存在（故障链接，HTTP 404），用于验证失败保留语义。
    content: str | None = ""


@dataclass
class Announcement:
    code: str                      # 公告编号
    title: str                     # 公告标题
    kind: str                      # original | correction
    project_name: str              # 项目名称
    published_at: str              # 发布时间 YYYY-MM-DD HH:MM
    deadline: str | None = None    # 投标截止时间（更正公告可为空）
    corrects_code: str | None = None  # 更正公告引用的原公告编号
    reason: str = ""               # 更正原因（仅更正公告）
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{self.code}.html"


# ---------------------------------------------------------------------------
# 示例数据：5 个项目，其中 3 个含更正公告，共 10 条公告
# ---------------------------------------------------------------------------
ANNOUNCEMENTS: list[Announcement] = [
    # 项目 A：原公告已过期，更正延期后仍在有效期内
    Announcement(
        code="SZ-2026-001",
        title="滨河路市政改造工程施工招标公告",
        kind="original",
        project_name="滨河路市政改造工程",
        published_at="2026-10-20 09:00",
        deadline="2026-11-05 14:30",
        attachments=[
            Attachment("SZ-2026-001-tender.txt", "招标文件.txt"),
            Attachment("SZ-2026-001-boq.txt", "工程量清单.txt"),
        ],
    ),
    Announcement(
        code="SZ-2026-001-G1",
        title="滨河路市政改造工程施工招标更正公告（第一次）",
        kind="correction",
        project_name="滨河路市政改造工程",
        published_at="2026-10-28 16:00",
        deadline="2026-11-25 14:30",
        corrects_code="SZ-2026-001",
        reason="因招标文件图纸调整，投标截止时间顺延。",
        attachments=[
            Attachment("SZ-2026-001-G1-clarification.txt", "澄清答疑.txt"),
        ],
    ),
    # 项目 B：两次更正，最终截止时间以发布时间最新的第二次更正为准
    Announcement(
        code="SZ-2026-002",
        title="城北污水处理厂扩建工程招标公告",
        kind="original",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-12 10:30",
        deadline="2026-11-10 09:00",
        attachments=[
            Attachment("SZ-2026-002-tender.txt", "招标文件.txt"),
        ],
    ),
    Announcement(
        code="SZ-2026-002-G1",
        title="城北污水处理厂扩建工程招标更正公告",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-10-22 15:00",
        deadline="2026-11-18 09:00",
        corrects_code="SZ-2026-002",
        reason="答疑澄清，截止时间调整。",
        attachments=[
            Attachment("SZ-2026-002-G1-qa.txt", "答疑澄清.txt"),
        ],
    ),
    Announcement(
        code="SZ-2026-002-G2",
        title="城北污水处理厂扩建工程招标更正公告（第二次）",
        kind="correction",
        project_name="城北污水处理厂扩建工程",
        published_at="2026-11-03 11:20",
        deadline="2026-11-30 10:00",
        corrects_code="SZ-2026-002",
        reason="再次延期，以本公告时间为准。",
        attachments=[
            Attachment("SZ-2026-002-G2-extension.txt", "延期说明.txt"),
        ],
    ),
    # 项目 C：无更正，且已过期
    Announcement(
        code="SZ-2026-003",
        title="东风桥加固维修工程招标公告",
        kind="original",
        project_name="东风桥加固维修工程",
        published_at="2026-09-15 08:45",
        deadline="2026-10-30 17:00",
        attachments=[
            Attachment("SZ-2026-003-tender.txt", "招标文件.txt"),
        ],
    ),
    # 项目 D：更正公告不修改截止时间（deadline 为空），生效时间仍取原公告
    Announcement(
        code="SZ-2026-004",
        title="高新区雨污分流管网工程招标公告",
        kind="original",
        project_name="高新区雨污分流管网工程",
        published_at="2026-10-25 14:00",
        deadline="2026-11-28 09:30",
        attachments=[
            Attachment("SZ-2026-004-tender.txt", "招标文件.txt"),
        ],
    ),
    Announcement(
        code="SZ-2026-004-G1",
        title="高新区雨污分流管网工程招标更正公告",
        kind="correction",
        project_name="高新区雨污分流管网工程",
        published_at="2026-11-01 09:10",
        deadline=None,
        corrects_code="SZ-2026-004",
        reason="仅更正联系人电话，投标截止时间不变。",
        attachments=[
            Attachment("SZ-2026-004-G1-contact.txt", "联系人变更说明.txt"),
            # 故障链接：该文件故意不生成，源站返回 404。
            # 用于验证“任一份附件失败时保留上一版完整可下载结果”。
            Attachment(
                "SZ-2026-004-G1-missing.txt",
                "补充说明（缺失）.txt",
                content=None,
            ),
        ],
    ),
    # 项目 E：原公告在最后一个列表页、更正公告在第一页（跨页乱序）
    Announcement(
        code="SZ-2026-005",
        title="湖滨公园配套市政道路工程招标公告",
        kind="original",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-10-08 13:00",
        deadline="2026-11-08 15:00",
        attachments=[
            Attachment("SZ-2026-005-tender.txt", "招标文件.txt"),
        ],
    ),
    Announcement(
        code="SZ-2026-005-G1",
        title="湖滨公园配套市政道路工程招标更正公告",
        kind="correction",
        project_name="湖滨公园配套市政道路工程",
        published_at="2026-11-05 17:30",
        deadline="2026-11-20 15:00",
        corrects_code="SZ-2026-005",
        reason="工程量清单调整，截止时间顺延。",
        attachments=[
            Attachment("SZ-2026-005-G1-boq.txt", "工程量清单（更正）.txt"),
        ],
    ),
]


def _detail_page(a: Announcement, revision: int = 1) -> str:
    rows = [
        ("公告编号", a.code),
        ("公告类型", "更正公告" if a.kind == "correction" else "招标公告"),
        ("项目名称", a.project_name),
        ("发布时间", a.published_at),
    ]
    if a.deadline is not None:
        rows.append(("投标截止时间", a.deadline))
    if a.kind == "correction":
        rows.append(("原公告编号", a.corrects_code or ""))
        rows.append(("更正事项", a.reason))

    trs = "\n".join(
        f'      <tr><th data-field="{html.escape(label)}">{html.escape(label)}</th>'
        f'<td data-value="{html.escape(label)}">{html.escape(value)}</td></tr>'
        for label, value in rows
    )

    attachment_items = "".join(
        f'      <li><a class="attachment-link" href="attachments/{html.escape(att.path)}"'
        f' data-filename="{html.escape(att.name)}">{html.escape(att.name)}</a></li>\n'
        for att in a.attachments
    )
    attachments_html = (
        f'    <h2>附件</h2>\n    <ul class="attachment-list">\n'
        f"{attachment_items}"
        f"    </ul>\n"
        if a.attachments
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(a.title)}</title>
</head>
<body>
  <article class="announcement-detail" data-code="{html.escape(a.code)}"
           data-kind="{a.kind}"
           data-published-at="{html.escape(a.published_at)}"
           {f'data-corrects="{html.escape(a.corrects_code)}"' if a.corrects_code else ""}>
    <h1 class="detail-title">{html.escape(a.title)}</h1>
    <table class="detail-table">
{trs}
    </table>
{attachments_html}    <p><a href="list_1.html">返回列表</a></p>
  </article>
</body>
</html>
"""


def _list_page(items: list[Announcement], page: int, total_pages: int) -> str:
    lis = []
    for a in items:
        lis.append(
            f'      <li class="announcement-item" data-code="{html.escape(a.code)}">\n'
            f'        <a class="detail-link" href="{html.escape(a.filename)}">{html.escape(a.title)}</a>\n'
            f'        <span class="published-at">{html.escape(a.published_at)}</span>\n'
            f'      </li>'
        )
    nav_parts = []
    for p in range(1, total_pages + 1):
        if p == page:
            nav_parts.append(f'<span class="page-current">{p}</span>')
        else:
            nav_parts.append(f'<a class="page-link" href="list_{p}.html">{p}</a>')
    next_link = (
        f'<a class="page-next" href="list_{page + 1}.html">下一页</a>'
        if page < total_pages
        else '<span class="page-next-disabled">下一页</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>市政工程公告站 - 第 {page} 页</title>
</head>
<body>
  <h1>市政工程招投标公告列表（第 {page}/{total_pages} 页）</h1>
  <ul class="announcement-list" data-page="{page}">
{chr(10).join(lis)}
  </ul>
  <nav class="pagination">
    {''.join(nav_parts)}
    {next_link}
  </nav>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成示例公告站")
    parser.add_argument(
        "--revision",
        type=int,
        default=1,
        help="附件内容版本号；再次生成时改用不同版本可验证内容变更",
    )
    args = parser.parse_args(argv)
    revision = args.revision

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)

    # 生成附件文件；content=None 的“故障链接”不写文件，由源站返回 404。
    attachment_count = 0
    for a in ANNOUNCEMENTS:
        for att in a.attachments:
            relative = Path(att.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"非法附件路径: {att.path}")
            target = ATTACHMENT_DIR / relative
            if att.content is None:
                # 确保故障链接对应的文件确实不存在（可能由旧生成残留）。
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            body = att.content or (
                f"公告编号: {a.code}\n"
                f"公告标题: {a.title}\n"
                f"附件: {att.name}\n"
                f"内容版本: {revision}\n"
            )
            target.write_text(body, encoding="utf-8")
            attachment_count += 1

    # 列表按发布时间倒序 —— 更正公告天然排在原公告前面，制造页面顺序干扰
    ordered = sorted(ANNOUNCEMENTS, key=lambda a: a.published_at, reverse=True)
    total_pages = (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE

    for a in ordered:
        (OUT_DIR / a.filename).write_text(_detail_page(a), encoding="utf-8")

    for page in range(1, total_pages + 1):
        chunk = ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        (OUT_DIR / f"list_{page}.html").write_text(
            _list_page(chunk, page, total_pages), encoding="utf-8"
        )

    (OUT_DIR / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=list_1.html">'
        "<title>公告站</title></head><body>跳转中…</body></html>\n",
        encoding="utf-8",
    )
    print(
        f"generated {len(ordered)} detail pages, {total_pages} list pages, "
        f"{attachment_count} attachment files in {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
