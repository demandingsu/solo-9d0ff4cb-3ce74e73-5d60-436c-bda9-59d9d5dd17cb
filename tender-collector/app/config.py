from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 本地默认落在项目 data/ 目录；Dockerfile 通过环境变量覆盖为 /data/tenders.db
_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "data" / "tenders.db")


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("TENDER_DB_PATH", _DEFAULT_DB)
    request_timeout: float = float(os.getenv("TENDER_HTTP_TIMEOUT", "15"))
    max_list_pages: int = int(os.getenv("TENDER_MAX_LIST_PAGES", "200"))
    user_agent: str = os.getenv(
        "TENDER_USER_AGENT", "TenderCollector/1.0 (+bid-deadline-monitor)"
    )


settings = Settings()
