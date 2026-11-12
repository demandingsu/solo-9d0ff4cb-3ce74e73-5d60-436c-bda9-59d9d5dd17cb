from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field


class CollectRequest(BaseModel):
    list_url: str = Field(
        ..., min_length=1, description="公告列表页地址（服务会自动跟随分页链接）"
    )


class ProjectOut(BaseModel):
    project_name: str
    current_deadline: str
    deadline_source_code: str
    deadline_source_url: str
    is_expired: bool


class CollectResponse(BaseModel):
    status: str
    source_url: str
    collected: int
    projects: list[ProjectOut]


class RunOut(BaseModel):
    id: int
    source_url: str
    status: str
    items_collected: int
    error: str | None
    started_at: str
    finished_at: str


# ---------------------------------------------------------------------------
# 附件归档
# ---------------------------------------------------------------------------
class ArchiveRequest(BaseModel):
    # 兼容 original_code / code / announcement_code 多种调用方式，
    # 对外文档以 original_code 为准。
    original_code: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices(
            "original_code", "code", "announcement_code"
        ),
        description="已采集的原公告编号",
    )


class AttachmentOut(BaseModel):
    id: int
    announcement_code: str
    filename: str
    url: str
    original_url: str
    source_url: str
    sha256: str
    digest: str
    size: int
    download_url: str


class ManifestOut(BaseModel):
    original_code: str
    project_name: str
    archived_at: str | None
    count: int
    archived: int
    attachments: list[AttachmentOut]


class ArchiveResponse(ManifestOut):
    status: str


class AttachmentRunOut(BaseModel):
    id: int
    original_code: str
    status: str
    items_archived: int
    error: str | None
    started_at: str
    finished_at: str
