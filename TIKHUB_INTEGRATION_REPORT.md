# TikHub 数据源接入报告

日期：2026-11-16。分支 `feature/tikhub-source`，基于 `feature/local-media-ui`（`1bf1be6`）。未合并 `master`。

## 结论

| 项目 | 状态 |
| --- | --- |
| TikHub Adapter、去重、下载接入、界面、测试 | **完成** |
| 免费 Demo 验证（真实请求，未使用 Token） | **通过** |
| 真实接口验收：公开视频经 TikHub 下载到本机并在播放器播放 | **BLOCKED** |

**验收标准尚未达成。** 本轮没有 TikHub Token，也没有付费请求的授权，因此没有发送任何付费请求，没有一条真实抖音视频经 TikHub 下载或播放。下面“已验证”的内容只说明代码、数据结构和界面流程；它们不能替代真实验收。

解除阻塞需要三件事，都在你的电脑上完成：在 `backend/.env` 配置 `TIKHUB_API_KEY`；确认可以发送首轮付费请求（上限见“首轮真实测试设计”）；提供 1 条有权处理的公开视频链接和 1 个博主主页链接。

## 五项单独报告

| 报告项 | 真实结果 | 说明 |
| --- | --- | --- |
| TikHub 成功解析的视频 | 付费接口 **0 条**（未执行） | 免费 Demo 的固定样例 1 条解析成功，仅用于核对结构，不计入。 |
| 真正下载成功且可播放的视频 | **0 条**（未执行） | 没有下载任何真实抖音视频，包括 Demo 样例：它不是你选择的视频。 |
| 需要登录/权限的视频 | **0 条**（未执行） | 分类逻辑已实现，只用合成数据测试过。 |
| API 请求量与费用 | 付费请求 **0 次，$0.000** | 本轮全部真实请求都是免费接口，明细见下。 |
| 原匿名方式与 TikHub 成功率对比 | 匿名 **0/2**；TikHub **未测** | 匿名数据来自 2026-10-23 的真实验收：两条抖音样例都被要求登录。TikHub 一侧没有数据，现在不能下结论。 |

真实验收后，运行 `uv run python scripts/tikhub_check.py report` 可离线生成这五项的实际数字；页面状态接口 `/api/network-media/tikhub/status` 的 `comparison` 字段给出同样的统计。

### 本轮发往 TikHub 的真实请求

全部不带 Token，全部免费。

| 请求 | 次数 | 结果 |
| --- | --- | --- |
| `/openapi.json`、文档首页 | 2 | 读取接口规范 |
| `/api/v1/health/check` | 1 | `{"status":"ok"}` |
| `/api/v1/demo/douyin/app/fetch_one_video` | 3 | 200；手工 1 次、检查脚本 1 次、页面“免费 Demo 测试”1 次 |
| `/api/v1/demo/douyin/web/fetch_one_video`、`/demo/cache_status` | 2 | 200；对照 Web 结构 |
| `/api/v1/tikhub/user/get_endpoint_info`、`get_tiered_discount_info` | 10 | 查询各接口公开单价 |
| `/api/v1/tikhub/user/get_user_info`（不带 Token） | 1 | 401；确认错误结构 |

## Demo 验证得到的事实

固定样例作品 `7534641277405531446`，通过本项目 Adapter 取得并映射：

- 响应为 `{"code": 200, "data": {"aweme_detail": {...}}}`，`aweme_detail` 约 197 个字段。
- 播放地址在 `video.play_addr`、`video.play_addr_h264`、`video.play_addr_265` 和 `video.bit_rate[].play_addr`，每项含多个镜像地址、`width`/`height`、`data_size`、`url_key`（含编码标记 `h264`/`bytevc1`/`bytevc2`）。
- 地址带签名，`video.cdn_url_expired` 给出过期时间；App 接口约两天，Web 接口约三小时。
- Adapter 从该样例选出 5 个候选：H.264 短边 576、10,328,237 bytes 的 3 个镜像，其后是 H.265 的 2 个镜像。
- 所有接口的成功响应都带同一句“本次请求将被计费”，包括免费的 Demo。这句话不能作为计费依据。
- 错误结构为 HTTP 状态码加 `{"detail": {"code": ...}}`。

没有请求过媒体地址本身，所以 **CDN 是否接受本项目的下载请求头尚未验证**。

## 实现

### 接口与单价

单价来自 2026-11-16 的 `get_endpoint_info`，是估算依据，不是账单。

| 用途 | 接口 | 单价 |
| --- | --- | --- |
| 分享短链接解析 | `GET /api/v1/douyin/app/v3/fetch_one_video_by_share_url` | $0.001 |
| 按作品 ID 查询详情 | `GET /api/v1/douyin/app/v3/fetch_one_video` | $0.001 |
| 博主公开视频列表（每页最多 20） | `GET /api/v1/douyin/app/v3/fetch_user_post_videos` | $0.001，不可用免费额度 |
| 主页短链接取 `sec_user_id` | `GET /api/v1/douyin/web/get_sec_user_id` | $0.001 |
| Token、余额检查 | `GET /api/v1/tikhub/user/get_user_info` | 免费 |
| 固定样例 | `GET /api/v1/demo/douyin/app/fetch_one_video` | 免费，不需 Token |

长链接中的作品 ID 和主页链接中的 `sec_user_id` 离线提取，不花请求。没有使用任何接受 Cookie 的接口，也没有使用收藏夹接口。

### 代码结构

| 文件 | 作用 |
| --- | --- |
| `backend/app/services/tikhub_adapter.py` | 独立 Adapter：HTTP 客户端、链接离线识别、响应映射、媒体地址选择。唯一接触 Token 的模块。 |
| `backend/app/services/tikhub_service.py` | 付费确认与每日上限、请求台账、预览、内存中的地址缓存、导入。 |
| `backend/app/services/network_library.py` | 按作品 ID 去重、导入、下载路径选择、统计。无 Token。 |
| `backend/app/services/network_download.py`、`network_download_worker.py` | 下载器接收已解析的公开地址，跳过 yt-dlp 提取，其余检查不变。 |
| `backend/app/api/routes/network_media.py` | `/tikhub/status`、`check`、`resolve`、`creator`、`import`；下载接口增加 `confirm_paid`、`force`、`method`。 |
| `frontend/src/pages/TikHubPanel.tsx`、`NetworkMediaLibrary.tsx`、`networkMediaApi.ts` | 连接状态、粘贴链接、博主列表、预览勾选、批量加入、失败原因。 |
| `backend/scripts/tikhub_check.py` | `demo`、`account`、`report` 三个免费命令。 |

ASR、AI 总结、RAG、笔记系统和整体界面布局没有改动。没有实现私人收藏夹同步。

### Token 与 Cookie

- Token 读自 `backend/.env` 的 `TIKHUB_API_KEY`，类型为 `SecretStr`；`.env` 已在 `.gitignore` 中。
- 只放在发往 `https://api.tikhub.io` 的 `Authorization` 请求头。服务地址只接受这一个官方主机；不跟随重定向。
- 不进入 URL、日志、错误信息、资料记录、台账、API 响应或下载子进程。前端只能读到 `configured: true/false`。
- Demo 请求不带 Token。账户检查不返回邮箱和密钥名称。
- 不读取、不索取、不上传抖音 Cookie。

### 去重

以 `douyin` 加平台作品 ID 为键。`/video/<id>`、`/note/<id>`、`iesdouyin.com/share/video/<id>`、`?modal_id=<id>` 离线识别为同一作品。短链接经 TikHub 解析后，把这个短链接记在对应资料上，再次粘贴不再请求。此前用“保存链接”存下的短链接或长链接会原地升级，标题、标签、备注和已下载媒体保留。

限制：两个从未解析过的不同短链接，无法离线判断是否指向同一作品，要到第一次通过 TikHub 下载时才会发现；那时第二条会以“已有本地副本”结束，不重复下载。

### 下载

| 要求 | 实现 |
| --- | --- |
| URL 与重定向校验 | 候选地址在后端和下载子进程各校验一次；重定向、DNS 固定和公网地址检查沿用原实现。 |
| 文件大小及超时 | 500 MiB、2 小时、15 分钟上限不变；`data_size` 超限的档位在选择时就排除。 |
| SHA-256 完整性 | 完整解码检查和 SHA-256 发布流程不变。 |
| 下载失败保留来源 | 不变；失败原因显示在列表卡片和详情。 |
| 已成功下载不重复请求 | TikHub 路径下返回 `already_downloaded`，不发请求、不重下；`force` 可显式覆盖。 |
| 记录请求数量及费用状态 | 台账 `network/tikhub/usage.json` 按日、按接口记录次数、成功/失败、估算计费；每次下载记录用了几次请求。 |

地址选择：优先 720p 及以下的 H.264，其次更高分辨率的 H.264，再次 H.265；不选 `bytevc2`。每次最多 6 个候选，逐个尝试。

配置了 Token 后，抖音来源默认走 TikHub；未配置时仍走原匿名方式。详情页保留“改用原匿名方式尝试”，便于对比。

### 付费确认

每个可能计费的操作，第一次请求都不带确认；后端在联系 TikHub 之前返回 `tikhub_confirmation_required`，界面显示次数和预估金额，确认后才发送。`TIKHUB_DAILY_REQUEST_LIMIT`（默认 20）在本机限制每日付费请求数。

## 首轮真实测试设计

未经你单独确认，不执行。上限：1 条视频详情、1 页博主列表（最多 20 条）、3 条你选择的视频下载。

准备：`TIKHUB_API_KEY` 写入 `backend/.env`，并设 `TIKHUB_DAILY_REQUEST_LIMIT=5`，让本机上限与首轮上限一致。

| 步骤 | 操作 | 付费请求 |
| --- | --- | --- |
| 0 | `uv run python scripts/tikhub_check.py account`，或页面“检查 Token 与余额” | 0 |
| 1 | “粘贴视频链接”解析 1 条公开视频 | 1 |
| 2 | “导入博主公开视频”，用 `https://www.douyin.com/user/…` 形式的主页链接取第一页 | 1 |
| 3 | 在两次预览中共勾选 3 条，“加入资料库”（先不下载） | 0 |
| 4 | 对照：逐条在详情点“改用原匿名方式尝试”，记录结果 | 0 |
| 5 | 逐条点“通过 TikHub 重试”（匿名已成功的那条会提示已有副本，无需再下） | 0；地址过期或后端重启后每条 1 |
| 6 | 在详情播放器播放每条成功的视频 | 0 |
| 7 | `uv run python scripts/tikhub_check.py report` | 0 |

匿名对照放在前面，是因为已有本地副本的作品不会再走 TikHub。预计 2 次请求、约 $0.002；最多 5 次、约 $0.005。博主列表接口不能用免费额度，需要账户有余额。

通过条件：至少 1 条视频状态为“已下载到本机”，播放器显示画面并能播放；`report` 的请求次数与 TikHub 后台用量一致。

需要记录：每条视频的状态和失败原因、文件大小与 SHA-256、播放器的时长和播放进度、`report` 输出、TikHub 后台的实际扣费。

## 验证

| 验证 | 结果 |
| --- | --- |
| 后端全量 | 1230 passed、1 skipped、2 xfailed，26.89 秒。skip 与 xfail 为既有项。 |
| 新增后端测试 | Adapter 56 项、服务与 API 22 项、下载器 14 项，均通过。 |
| 前端全量 | 106 passed；其中新增 9 项。 |
| TypeScript 与 Vite 构建 | 成功。 |
| 依赖锁 | `uv lock --check` 通过，未新增依赖。 |
| 真实页面 | 未配置 Token 的后端加开发前端：状态显示“未配置 Token”，“免费 Demo 测试”真实返回样例，付费请求计数 0。 |
| 模拟流程 | 合成的 TikHub 响应加 ffmpeg 生成的测试片段：确认、预览、加入并下载、播放器播放（时长 8 秒，`readyState=4`，进度前进）、长短链接去重、博主列表勾选、私密作品提示。 |

模拟流程中的 TikHub 响应和视频文件都是合成的，只证明界面和后端的衔接，不证明 TikHub 付费接口或抖音 CDN 的真实行为。下载器测试中有一项用本机 ffmpeg 生成的片段走完真实的探测和完整解码。

## 尚未验证的假设

1. **CDN 请求头。** 下载带 `Referer: https://www.douyin.com/` 和桌面浏览器 UA。真实 CDN 是否接受，要等首轮测试。
2. **计费规则。** 台账按“HTTP 200 且 `code` 为 200 才计费”估算，需与 TikHub 后台对账。
3. **无权限作品的返回。** `filter_list[0].reason` 的 5、10、8 分别映射为私密、部分可见、不存在或受限，依据是接口文档，没有见过真实样本。
4. **翻页。** `has_more` 与 `max_cursor` 的用法依据文档；只在首轮测试中取第一页。
5. **H.265 回退。** 没有 H.264 档位时下载 H.265，浏览器可能不能播放；界面会提示下载原件。
6. **海外节点。** TikHub 出口在海外，返回的可能是海外 CDN 地址。指定地区的接口每次 $0.005，本轮没有使用。
7. **封面。** 预览封面由浏览器直接从抖音图片服务器加载，加载失败时显示占位图标。

## 本轮运行环境的限制

本轮在云端容器中完成。容器只放行了 `api.tikhub.io` 和 `docs.tikhub.io`，访问不到抖音 CDN；下载子进程按设计不使用代理，在这个容器里也无法直连外网。因此即使有 Token，“下载到本机并播放”也必须在你的电脑上验收。
