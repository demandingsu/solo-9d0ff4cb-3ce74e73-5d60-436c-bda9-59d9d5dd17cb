from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from . import database
from .archive import (
    ArchiveError,
    ArchiveNotFound,
    archive_attachments,
    get_manifest,
    resolve_attachment,
)
from .models import (
    ArchiveRequest,
    ArchiveResponse,
    AttachmentRunOut,
    CollectRequest,
    CollectResponse,
    ManifestOut,
    ProjectOut,
    RunOut,
)
from .service import ServiceError, query_projects, run_collection


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程投标截止时间采集 API",
    description=(
        "提交公告列表页地址，服务抓取全部详情页，将更正公告按引用的原公告编号归并，"
        "按发布时间确定生效的投标截止时间。重复采集不产生重复记录；"
        "任一详情页读取失败时整体报错并保留上次成功采集结果。"
        "另提供附件归档：按原公告编号重新读取其与原公告及更正的详情页，"
        "从同站下载附件并以 SHA-256 保存；全部成功后才整体替换可下载清单。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未入库
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(status="success", **result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects()]


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]


# ---------------------------------------------------------------------------
# 附件归档 / 清单 / 下载
# ---------------------------------------------------------------------------
def _archive(original_code: str) -> ArchiveResponse:
    try:
        result = archive_attachments(original_code)
    except ArchiveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ArchiveError as exc:
        # 422 语义：源站详情/附件不可读或超限，本次未替换可见清单
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ArchiveResponse(status="success", **result)


@app.post("/api/attachments/archive", response_model=ArchiveResponse)
def archive_endpoint(req: ArchiveRequest) -> ArchiveResponse:
    return _archive(req.original_code)


# 便捷别名，便于脚本/离线工具调用。
@app.post("/api/archive", response_model=ArchiveResponse)
def archive_endpoint_alias(req: ArchiveRequest) -> ArchiveResponse:
    return _archive(req.original_code)


@app.post("/api/archive/{original_code}", response_model=ArchiveResponse)
def archive_endpoint_path(original_code: str) -> ArchiveResponse:
    return _archive(original_code)


@app.post(
    "/api/attachments/archive/{original_code}",
    response_model=ArchiveResponse,
)
def archive_endpoint_attachments_path(original_code: str) -> ArchiveResponse:
    return _archive(original_code)


@app.post("/api/archive-attachments", response_model=ArchiveResponse)
def archive_endpoint_flat_alias(req: ArchiveRequest) -> ArchiveResponse:
    return _archive(req.original_code)


@app.post(
    "/api/announcements/{original_code}/attachments/archive",
    response_model=ArchiveResponse,
)
def archive_endpoint_nested(original_code: str) -> ArchiveResponse:
    return _archive(original_code)


@app.get("/api/attachments", response_model=ManifestOut)
def attachments_endpoint(
    original_code: str | None = Query(None, description="原公告编号"),
    code: str | None = Query(None, description="original_code 的别名"),
    announcement_code: str | None = Query(
        None, description="original_code 的别名"
    ),
) -> ManifestOut:
    value = original_code or code or announcement_code
    if not value:
        raise HTTPException(
            status_code=422, detail="缺少原公告编号（original_code）"
        )
    try:
        return ManifestOut(**get_manifest(value))
    except ArchiveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/announcements/{original_code}/attachments",
    response_model=ManifestOut,
)
def attachments_endpoint_nested(original_code: str) -> ManifestOut:
    try:
        return ManifestOut(**get_manifest(original_code))
    except ArchiveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/archive/{original_code}", response_model=ManifestOut)
def attachments_endpoint_alias(original_code: str) -> ManifestOut:
    try:
        return ManifestOut(**get_manifest(original_code))
    except ArchiveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/projects/{original_code}/attachments",
    response_model=ManifestOut,
)
def attachments_endpoint_project(original_code: str) -> ManifestOut:
    try:
        return ManifestOut(**get_manifest(original_code))
    except ArchiveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _download(attachment_id: int) -> FileResponse:
    try:
        path, filename, sha256 = resolve_attachment(attachment_id)
    except ArchiveNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"X-Content-SHA256": sha256},
    )


@app.get("/api/attachments/{attachment_id}/download")
def download_endpoint(attachment_id: int) -> FileResponse:
    return _download(attachment_id)


@app.get("/api/attachments/{attachment_id}/file")
def download_endpoint_alias(attachment_id: int) -> FileResponse:
    return _download(attachment_id)


@app.get("/api/attachments/download/{attachment_id}")
def download_endpoint_alt(attachment_id: int) -> FileResponse:
    return _download(attachment_id)


@app.get("/api/download/{attachment_id}")
def download_endpoint_flat(attachment_id: int) -> FileResponse:
    return _download(attachment_id)


@app.get("/api/attachment-runs", response_model=list[AttachmentRunOut])
def attachment_runs_endpoint(
    original_code: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
) -> list[AttachmentRunOut]:
    with database.get_conn() as conn:
        rows = database.fetch_attachment_runs(conn, original_code, limit)
        return [AttachmentRunOut(**dict(r)) for r in rows]
