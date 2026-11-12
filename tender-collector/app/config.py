from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 本地默认落在项目 data/ 目录；Dockerfile 通过环境变量覆盖为 /data/tenders.db
_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "data" / "tenders.db")
_DEFAULT_ATTACHMENT_DIR = str(
    Path(os.getenv("TENDER_DB_PATH", _DEFAULT_DB)).resolve().parent / "attachments"
)

# 单个附件上限：10 MiB（按实际写入磁盘的字节数计算）
DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("TENDER_DB_PATH", _DEFAULT_DB)
    request_timeout: float = float(os.getenv("TENDER_HTTP_TIMEOUT", "15"))
    max_list_pages: int = int(os.getenv("TENDER_MAX_LIST_PAGES", "200"))
    user_agent: str = os.getenv(
        "TENDER_USER_AGENT", "TenderCollector/1.0 (+bid-deadline-monitor)"
    )
    attachment_dir: str = os.getenv(
        "TENDER_ATTACHMENT_DIR", _DEFAULT_ATTACHMENT_DIR
    )
    max_attachment_bytes: int = int(
        os.getenv(
            "TENDER_ATTACHMENT_MAX_BYTES", str(DEFAULT_MAX_ATTACHMENT_BYTES)
        )
    )


settings = Settings()
