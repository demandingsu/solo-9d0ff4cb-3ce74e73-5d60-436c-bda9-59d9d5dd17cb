# 市政工程投标截止时间采集服务

面向招投标专员的公告采集 API：提交市政工程公告站的**列表页地址**，服务自动遍历
所有分页与详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布
时间**（而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。

同时提供**附件归档**：给定已采集的原公告编号，重新读取该公告及其更正公告的
详情页，把详情中的同站附件逐份下载并以 SHA-256 保存，供无法访问公告站的同事
离线下载。只有一轮中所有详情与附件都成功读取后才整体替换对外可见清单；任一
失败都保留上一版完整可下载结果。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。
7. **附件归档（整轮成功才切换）**：按原公告编号重新读取原公告及全部已采集更正的
   详情页，收集附件链接并逐份从**同源**下载；所有详情与附件都成功后才在单个
   数据库事务内整体替换该项目的可见附件清单。任一份详情或附件读取失败时，
   上一版清单与内容文件保持完整可下载。
8. **内容寻址与源站变化**：文件按 SHA-256 摘要保存；同一链接内容变化会得到新的
   摘要和新的文件。成功重跑后，源站已删除的链接会从清单中消失；单文件上限
   10 MiB（可配置），超限视为本轮失败且不替换旧清单。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：采集/查询/附件归档/清单/下载
│   ├── service.py          # 采集编排 + 事务边界 + 过期判定
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析
│   ├── archive.py          # 附件归档编排、同源校验、SHA-256 存储
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── database.py         # SQLite 连接、建表、UPSERT、附件清单事务替换
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 生成的本地分页 HTML 示例公告站（静态文件）
│   └── attachments/        # 示例附件（按生成器写入）
├── scripts/generate_demo_site.py  # 示例站生成器（数据可复现）
├── tests/test_collection.py       # 采集/归并端到端测试
├── tests/test_archive.py          # 附件归档/失败保留/内容变更测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 归档某个原公告及其更正公告的附件（全部成功才替换可见清单）
curl -X POST http://localhost:8000/api/attachments/archive \
  -H 'Content-Type: application/json' \
  -d '{"original_code": "SZ-2026-001"}'

# 6. 查看当前可见附件清单
curl 'http://localhost:8000/api/attachments?original_code=SZ-2026-001'

# 7. 下载清单中的某份附件（id 来自上一步）
curl -OJ http://localhost:8000/api/attachments/1/download
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html>。SQLite 数据与归档内容存于
命名卷 `tender-data`（容器内 `/data/tenders.db` 与 `/data/attachments/`）。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站
python3 -m http.server 8001 --directory demo_site

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'

# 归档附件（示例站与 API 均可离线访问，不依赖外网）
curl -X POST http://localhost:8000/api/attachments/archive \
  -H 'Content-Type: application/json' \
  -d '{"original_code": "SZ-2026-001"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

### `POST /api/attachments/archive` — 归档原公告及其更正的附件

请求（`original_code` 也可写作 `code`；另有路径形式
`POST /api/archive/{original_code}` 与
`POST /api/announcements/{original_code}/attachments/archive`）：

```json
{ "original_code": "SZ-2026-001" }
```

行为：

1. 校验该编号是数据库中已采集的**原公告**（传更正编号或未采集编号返回 404）；
2. 重新抓取该原公告及所有引用它的已采集更正公告的详情页；
3. 解析详情中的附件链接，逐份从**同源**下载（跨站链接按失败处理）；
4. 对每份文件流式计算 SHA-256，按摘要保存内容，并记录所属公告、原始链接、
   文件名、字节数与摘要；
5. 全部成功后，在单个事务内整体替换该原公告编号下的可见附件清单。

成功响应（200）与清单接口结构一致，另含 `status`：

```json
{
  "status": "success",
  "original_code": "SZ-2026-001",
  "project_name": "滨河路市政改造工程",
  "archived_at": "2026-11-12T02:40:00+00:00",
  "count": 3,
  "archived": 3,
  "attachments": [
    {
      "id": 1,
      "announcement_code": "SZ-2026-001",
      "filename": "招标文件.txt",
      "url": "http://demo-site/attachments/SZ-2026-001-tender.txt",
      "original_url": "http://demo-site/attachments/SZ-2026-001-tender.txt",
      "source_url": "http://demo-site/attachments/SZ-2026-001-tender.txt",
      "sha256": "……",
      "digest": "……",
      "size": 127,
      "download_url": "/api/attachments/1/download"
    }
  ]
}
```

失败响应（422）：任一详情页或附件读取失败（网络错误、非 2xx、超过单文件上限、
链接不在同站）时返回，例如：

```json
{ "detail": "读取附件失败 http://demo-site/attachments/SZ-2026-001-boq.txt: HTTP 404" }
```

此时数据库中该项目的附件清单与所有内容文件保持上一版，下载接口仍可正常使用；
失败原因记入 `attachment_runs` 表。

### `GET /api/attachments?original_code=...` — 当前可见附件清单

返回当前对外可见的附件清单（未归档过时为空清单）。别名：
`GET /api/announcements/{original_code}/attachments`。

### `GET /api/attachments/{attachment_id}/download` — 下载附件

下载当前清单中某个附件，响应头 `X-Content-SHA256` 给出内容摘要，
`Content-Disposition` 给出文件名。同一链接内容变化时附件 id 与下载地址保持不变，
但下载得到新内容；源站删除链接后，对应旧附件 id 返回 404。

### `GET /api/attachment-runs?original_code=...&limit=20` — 附件归档运行历史

记录每轮附件归档的成功/失败、归档条数与错误信息。

### 附件归档的可见性语义

- **整轮原子切换**：新内容先完整下载并落盘，之后才在单事务内替换清单；
  提交前对外只有旧清单，任一失败时旧清单与旧文件不被触碰。
- **源站删除链接**：成功重跑会按最新详情页重建清单，已删除链接不再出现。
- **同一链接内容变更**：内容按 SHA-256 寻址，新内容得到新摘要、新文件，
  下载接口返回新内容，不会命中旧缓存。
- **单文件上限**：默认 10 MiB，按实际写入字节数检查（流式读取，不把整个文件
  载入内存）；超限视为本轮失败。
- **同源约束**：附件链接必须与详情页面同源，不跟随跨站重定向，避免把外部站点
  内容纳入归档。

## 示例站数据设计

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

示例站每个公告详情页还包含 `ul.attachment-list`，其中
`a.attachment-link` 指向 `demo_site/attachments/` 下的真实文件。生成器会写出
11 份附件，并特意在 **SZ-2026-004-G1**（高新区雨污分流管网工程的更正公告）
放一个源站文件不存在的**故障链接**
`attachments/SZ-2026-004-G1-missing.txt`：归档 `SZ-2026-004` 会返回 422 并保留
上一版结果，补齐该文件后重跑即可成功。

验证“同一链接内容变更”时，可以换一个版本号重新生成示例附件（默认版本 1）：

```bash
python3 scripts/generate_demo_site.py --revision 2
```

随后再次调用归档接口，清单中的 `sha256`/`size` 会变化，下载得到新内容。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_ATTACHMENT_DIR` | 与数据库同目录下的 `attachments/`；容器内 `/data/attachments` | SHA-256 内容文件存储目录 |
| `TENDER_ATTACHMENT_MAX_BYTES` | `10485760`（10 MiB） | 单份附件大小上限（字节） |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面/附件请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py`（采集）或
`app/archive.py`（附件）即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）；
- 附件：详情容器内的 `a.attachment-link[href]`、`a[data-attachment][href]`，
  或 `.attachment-list` / `.attachments` 容器中的普通链接；文件名优先取
  `data-filename`，其次取链接文本。此外，详情容器内带常见附件后缀
  （如 `.pdf`、`.docx`、`.xlsx`、`.zip` 等）的普通链接也会被识别。
