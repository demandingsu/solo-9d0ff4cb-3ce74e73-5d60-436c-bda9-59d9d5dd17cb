"""附件归档端到端测试。

每个测试使用 demo_site 的临时副本，避免修改仓库中的示例站；静态服务器支持
按路径注入 404，用于验证失败保留语义。
"""
from __future__ import annotations

import hashlib
import http.server
import importlib
import shutil
import threading
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "demo_site"


def _make_handler(site_dir: Path, fail_paths: set[str]):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site_dir), **kwargs)

        def do_GET(self):  # noqa: N802
            if self.path in fail_paths:
                self.send_error(404, "injected failure")
                return
            return super().do_GET()

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture()
def site_server(tmp_path):
    site_dir = tmp_path / "site"
    shutil.copytree(DEMO_DIR, site_dir)

    fail_paths: set[str] = set()
    handler = _make_handler(site_dir, fail_paths)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, site_dir, fail_paths
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TENDER_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TENDER_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    from app import config, database
    importlib.reload(config)
    import app.main as main
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c, database


def _collect(client: TestClient, base: str) -> None:
    resp = client.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
    assert resp.status_code == 200, resp.text


def _archive(client: TestClient, code: str, expect: int = 200) -> dict:
    resp = client.post(
        "/api/attachments/archive", json={"original_code": code}
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


def _by_filename(payload: dict) -> dict[str, dict]:
    return {a["filename"]: a for a in payload["attachments"]}


def _download(client: TestClient, att: dict) -> bytes:
    resp = client.get(att["download_url"])
    assert resp.status_code == 200, resp.text
    return resp.content


# ---------------------------------------------------------------------------
# 成功归档与下载
# ---------------------------------------------------------------------------
def test_archive_and_download_all_attachments(client, site_server):
    c, _ = client
    base, site_dir, _ = site_server
    _collect(c, base)

    payload = _archive(c, "SZ-2026-001")
    assert payload["status"] == "success"
    assert payload["original_code"] == "SZ-2026-001"
    assert payload["project_name"] == "滨河路市政改造工程"
    assert payload["count"] == 3

    by_name = _by_filename(payload)
    assert set(by_name) == {"招标文件.txt", "工程量清单.txt", "澄清答疑.txt"}
    assert by_name["澄清答疑.txt"]["announcement_code"] == "SZ-2026-001-G1"

    for att in payload["attachments"]:
        content = _download(c, att)
        assert hashlib.sha256(content).hexdigest() == att["sha256"]
        assert len(content) == att["size"]
        assert att["url"].startswith(base + "/attachments/")

    # 清单接口返回同一份可见结果
    manifest = c.get("/api/attachments?original_code=SZ-2026-001").json()
    assert manifest["count"] == 3
    assert manifest["archived_at"] is not None
    assert [a["sha256"] for a in manifest["attachments"]] == [
        a["sha256"] for a in payload["attachments"]
    ]


def test_archive_reads_corrections_not_just_original(client, site_server):
    c, _ = client
    base, _, _ = site_server
    _collect(c, base)

    payload = _archive(c, "SZ-2026-002")
    names = {a["filename"] for a in payload["attachments"]}
    assert names == {"招标文件.txt", "答疑澄清.txt", "延期说明.txt"}
    assert {a["announcement_code"] for a in payload["attachments"]} == {
        "SZ-2026-002",
        "SZ-2026-002-G1",
        "SZ-2026-002-G2",
    }


# ---------------------------------------------------------------------------
# 重跑反映源站变化
# ---------------------------------------------------------------------------
def test_successful_rerun_removes_deleted_link(client, site_server):
    c, _ = client
    base, site_dir, _ = site_server
    _collect(c, base)

    first = _archive(c, "SZ-2026-001")
    removed = _by_filename(first)["澄清答疑.txt"]

    # 源站删除该附件链接
    detail = site_dir / "SZ-2026-001-G1.html"
    soup = BeautifulSoup(detail.read_text(encoding="utf-8"), "html.parser")
    for a in soup.select("a.attachment-link"):
        if a.get("href", "").endswith("-clarification.txt"):
            a.find_parent("li").decompose()
    detail.write_text(str(soup), encoding="utf-8")

    second = _archive(c, "SZ-2026-001")
    assert second["count"] == 2
    assert "澄清答疑.txt" not in _by_filename(second)

    # 旧附件不再对外可见/下载
    assert c.get(f"/api/attachments/{removed['id']}/download").status_code == 404


def test_changed_content_produces_new_digest_and_file(client, site_server):
    c, _ = client
    base, site_dir, _ = site_server
    _collect(c, base)

    first = _by_filename(_archive(c, "SZ-2026-001"))["招标文件.txt"]
    old_content = _download(c, first)

    source = site_dir / "attachments" / "SZ-2026-001-tender.txt"
    source.write_bytes("内容已被源站替换".encode("utf-8"))

    second = _by_filename(_archive(c, "SZ-2026-001"))["招标文件.txt"]
    assert second["url"] == first["url"]
    # 同一链接的清单条目保留稳定 id，但摘要与内容必须是新的
    assert second["id"] == first["id"]
    assert second["sha256"] != first["sha256"]
    assert second["size"] != first["size"]
    new_content = _download(c, second)
    assert new_content == "内容已被源站替换".encode("utf-8")
    assert new_content != old_content
    # 通过第一次拿到的同一个下载地址，也能得到新文件
    assert _download(c, first) == new_content


# ---------------------------------------------------------------------------
# 失败保留上一版完整可下载结果
# ---------------------------------------------------------------------------
def test_attachment_failure_keeps_previous_manifest_and_files(
    client, site_server
):
    c, _ = client
    base, site_dir, fail_paths = site_server
    _collect(c, base)

    first = _archive(c, "SZ-2026-001")
    first_ids = {a["filename"]: a["id"] for a in first["attachments"]}

    # 源站同时发生两件事：一份附件内容已变化、另一份附件 404。
    (site_dir / "attachments" / "SZ-2026-001-tender.txt").write_bytes(b"changed")
    fail_paths.add("/attachments/SZ-2026-001-boq.txt")
    try:
        resp = c.post(
            "/api/attachments/archive", json={"original_code": "SZ-2026-001"}
        )
        assert resp.status_code == 422
        assert "SZ-2026-001-boq.txt" in resp.json()["detail"]
    finally:
        fail_paths.discard("/attachments/SZ-2026-001-boq.txt")

    # 上一版清单、摘要和内容都还在
    manifest = c.get("/api/attachments?original_code=SZ-2026-001").json()
    assert manifest["count"] == 3
    for name, att_id in first_ids.items():
        assert att_id in {a["id"] for a in manifest["attachments"]}
        download = c.get(f"/api/attachments/{att_id}/download")
        assert download.status_code == 200
        expected = next(
            a["sha256"] for a in first["attachments"] if a["id"] == att_id
        )
        assert hashlib.sha256(download.content).hexdigest() == expected


def test_detail_failure_keeps_previous_manifest(client, site_server):
    c, _ = client
    base, _, fail_paths = site_server
    _collect(c, base)

    first = _archive(c, "SZ-2026-001")
    first_digests = [a["sha256"] for a in first["attachments"]]

    fail_paths.add("/SZ-2026-001-G1.html")
    try:
        resp = c.post(
            "/api/attachments/archive", json={"original_code": "SZ-2026-001"}
        )
        assert resp.status_code == 422
        assert "SZ-2026-001-G1.html" in resp.json()["detail"]
    finally:
        fail_paths.discard("/SZ-2026-001-G1.html")

    manifest = c.get("/api/attachments?original_code=SZ-2026-001").json()
    assert [a["sha256"] for a in manifest["attachments"]] == first_digests
    for att in manifest["attachments"]:
        assert c.get(att["download_url"]).status_code == 200


def test_fault_link_in_demo_site_preserves_empty_then_recovers(
    client, site_server
):
    c, _ = client
    base, site_dir, _ = site_server
    _collect(c, base)

    # SZ-2026-004-G1 故意包含一个 404 附件链接
    resp = c.post(
        "/api/attachments/archive", json={"original_code": "SZ-2026-004"}
    )
    assert resp.status_code == 422
    assert "SZ-2026-004-G1-missing.txt" in resp.json()["detail"]

    # 从未成功归档过：清单为空，但接口本身正常
    manifest = c.get("/api/attachments?original_code=SZ-2026-004").json()
    assert manifest["count"] == 0
    assert manifest["archived_at"] is None

    # 源站补齐文件后，成功重跑
    (site_dir / "attachments" / "SZ-2026-004-G1-missing.txt").write_text(
        "补充材料", encoding="utf-8"
    )
    payload = _archive(c, "SZ-2026-004")
    assert payload["count"] == 3


# ---------------------------------------------------------------------------
# 单文件上限
# ---------------------------------------------------------------------------
def test_single_file_limit_is_10_mib_by_default():
    from app.config import DEFAULT_MAX_ATTACHMENT_BYTES

    assert DEFAULT_MAX_ATTACHMENT_BYTES == 10 * 1024 * 1024


def test_oversized_attachment_rejected_and_previous_kept(
    client, site_server, monkeypatch
):
    c, _ = client
    base, site_dir, _ = site_server
    _collect(c, base)

    first = _archive(c, "SZ-2026-001")
    first_digests = [a["sha256"] for a in first["attachments"]]

    monkeypatch.setenv("TENDER_ATTACHMENT_MAX_BYTES", "1024")
    source = site_dir / "attachments" / "SZ-2026-001-tender.txt"
    source.write_bytes(b"x" * 1025)

    resp = c.post(
        "/api/attachments/archive", json={"original_code": "SZ-2026-001"}
    )
    assert resp.status_code == 422
    assert "上限" in resp.json()["detail"]

    manifest = c.get("/api/attachments?original_code=SZ-2026-001").json()
    assert [a["sha256"] for a in manifest["attachments"]] == first_digests

    # 恰好等于上限可以归档
    monkeypatch.setenv("TENDER_ATTACHMENT_MAX_BYTES", "1025")
    payload = _archive(c, "SZ-2026-001")
    assert payload["count"] == 3


# ---------------------------------------------------------------------------
# 输入校验与接口别名
# ---------------------------------------------------------------------------
def test_all_links_removed_yields_empty_visible_manifest(client, site_server):
    c, _ = client
    base, site_dir, _ = site_server
    _collect(c, base)

    first = _archive(c, "SZ-2026-001")
    old_ids = [a["id"] for a in first["attachments"]]

    # 删除原公告与更正公告中的全部附件链接
    for filename in ("SZ-2026-001.html", "SZ-2026-001-G1.html"):
        detail = site_dir / filename
        soup = BeautifulSoup(detail.read_text(encoding="utf-8"), "html.parser")
        for a in soup.select(".attachment-list a"):
            a.find_parent("li").decompose()
        detail.write_text(str(soup), encoding="utf-8")

    second = _archive(c, "SZ-2026-001")
    assert second["count"] == 0
    assert second["attachments"] == []
    assert second["archived_at"] is not None
    for old_id in old_ids:
        assert c.get(f"/api/attachments/{old_id}/download").status_code == 404


def test_cross_origin_attachment_link_rejected_without_fetch(
    client, site_server
):
    c, _ = client
    base, site_dir, _ = site_server
    _collect(c, base)
    _archive(c, "SZ-2026-001")

    detail = site_dir / "SZ-2026-001.html"
    html_text = detail.read_text(encoding="utf-8")
    html_text = html_text.replace(
        "</ul>",
        '<li><a class="attachment-link" '
        'href="http://example.invalid/evil.txt">evil</a></li></ul>',
        1,
    )
    detail.write_text(html_text, encoding="utf-8")

    resp = c.post(
        "/api/attachments/archive", json={"original_code": "SZ-2026-001"}
    )
    assert resp.status_code == 422
    assert "不在同站" in resp.json()["detail"]
    # 失败时旧清单仍完整
    assert c.get("/api/attachments?original_code=SZ-2026-001").json()["count"] == 3


def test_unknown_or_correction_code_returns_404(client, site_server):
    c, _ = client
    base, _, _ = site_server
    _collect(c, base)

    for code in ("SZ-2026-999", "SZ-2026-001-G1"):
        resp = c.post(
            "/api/attachments/archive", json={"original_code": code}
        )
        assert resp.status_code == 404, resp.text

    assert c.get("/api/attachments?original_code=NOPE").status_code == 404


def test_archive_body_and_path_aliases(client, site_server):
    c, _ = client
    base, _, _ = site_server
    _collect(c, base)

    resp = c.post("/api/archive", json={"code": "SZ-2026-003"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1

    resp = c.post("/api/archive/SZ-2026-003")
    assert resp.status_code == 200, resp.text

    resp = c.post("/api/announcements/SZ-2026-005/attachments/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 2
    assert c.get("/api/announcements/SZ-2026-005/attachments").status_code == 200
