# Phase 0 技术验证与复用审查

审查日期：2026-10-22 至 2026-10-23（Asia/Tokyo）。安装与主测试在 22 日执行，报告于跨日后完成。范围：实际运行既有项目、离线验证、确定最小改造边界；本报告不是新产品实施或真实视频端到端验收。

## 1. 结论与审查基线

**建议继续在 Fork 内开发一个本地音视频 CLI 验证切片，暂不抽取源码另建独立产品。** 现有音频处理、分段恢复、原始结果保存、知识库归类、证据检索与 Markdown 导出有复用价值。多来源入口、稳定来源身份、人工笔记保护和持久任务机制尚不完整。README 声明 MIT，但没有独立许可证文本；按本阶段约束，不把源码迁入新独立产品。

实际完成：

- 原有前端：72 passed / 0 failed / 0 skipped；TypeScript + Vite 构建成功。
- 原有后端：首次因 Windows 默认 pytest 临时目录权限发生 880 setup errors；仅改运行命令指定新的临时目录后，880 项中 **879 passed / 0 failed / 0 errors / 1 skipped**。没有修改实现、测试断言或 skip 条件。
- 真实 Uvicorn 本地进程启动成功，健康检查、OpenAPI 与空库统计均返回 HTTP 200。
- 合成 fixture → 正式音频入库编排 → 真实分块 → 真实 SQLite / Chroma → Markdown 的离线集成通过。下载、ASR、embedding 与笔记 LLM 均为 mock，不能称为真实视频全链路成功。
- 另外动态复现两项缺陷：跨收藏夹同视频唯一约束冲突；未归类视频取消收藏后留下孤儿 Chroma 数据。它们不计入原有 pytest 的失败数量。
- npm audit 报告 8 项漏洞（6 high / 2 moderate）；未执行自动升级或 audit fix。

开始时工作目录为空。确认 GitHub 下尚无目标 Fork 后，按用户补充要求创建并克隆：

| 项目 | 值 |
| --- | --- |
| 上游 | https://github.com/MQTQW/douyinmind |
| Fork | https://github.com/baigneuse/douyinmind |
| 本地目录 | D:\新建文件夹\douyinmind |
| 基线 commit | 4f0bfbf5129cba45e8575562606df9aa04f7ed0b |
| 分支 | master |
| origin / upstream | baigneuse/douyinmind / MQTQW/douyinmind |
| 仓库工作指令 | 根及 tracked tree 未发现 AGENTS.md |
| 已读文档 | README.md、docs/architecture.md、docs/usage.md，并核对 audio/evaluation 文档 |
| 本次源码变动 | 没有修改既有业务实现、UI、测试或依赖声明；新增本审查、离线 probe，保留安装生成的 backend/uv.lock |
| 远端写入 | 仅创建 Fork；本次审查和 probe 保留在本地，未提交或推送 |

GitHub 身份仅用于用户明确要求的 Fork 操作。没有使用视频平台个人账号、Cookie 或真实 API Key，没有登录抖音、采集真实收藏、上传个人媒体、调用真实模型或配置生产设施。

## 2. 真实模块依赖与调用流程

下图来自实际 import 和函数调用，而不是规划文件：

~~~mermaid
flowchart TD
    UI["React Workspace / frontend/src/api.ts"] --> API["FastAPI /api"]

    API --> Login["POST /auth/douyin/login/start"]
    Login --> Collector["DouyinCollector.start_login"]
    Collector --> Browser["Playwright persistent Chromium + 抖音登录"]
    Browser --> Scrape["网页 Webpack 收藏接口"]
    Scrape --> Snapshot["内存 FavoriteScrapeSnapshot"]
    API --> FavSync["POST /favorites/sync"]
    FavSync --> FavService["FavoritesService.sync_from_douyin"]
    Snapshot --> FavService
    FavService --> SQL["SQLite / SQLAlchemy"]

    API --> Ingest["POST /knowledge/sync"]
    Ingest --> Start["KnowledgeService.start_sync"]
    Start --> Worker["Worker 内存队列 + 后台线程"]
    Worker --> Process["KnowledgeService._process_videos"]
    SQL --> Process
    Process --> URL["platform_item_id 拼接抖音 URL"]
    URL --> Download["默认 download_audio / 可选 download_video"]
    Download --> Media["抖音专用下载 / 下载函数内通用 yt-dlp 分支"]
    Media --> Audio["ffmpeg 规范化与分段"]
    Audio --> Omni["OmniService.analyze_audio / analyze_video"]
    Omni --> Qwen["外部 Qwen Omni"]
    Omni <--> Checkpoint["音频指纹 / 分段 checkpoint"]
    Omni --> Result["VideoAnalysis + timed segments + raw_results"]
    Result --> SQL
    Result --> Chunk["knowledge_chunks → build_fixed_chunks"]
    Chunk --> Embed["外部 DashScope embedding"]
    Embed --> Chroma["本地 Chroma PersistentClient"]

    API --> Chat["POST /chat/ask 或 /chat/ask/stream"]
    Chat --> RAG["RagService.answer / answer_stream"]
    RAG --> Scope["知识库范围 / 问题分流 / 追问线索"]
    Scope --> QueryEmbed["外部 query embedding"]
    QueryEmbed --> Chroma
    Chroma --> Fusion["范围内正文 + semantic / lexical fusion"]
    Fusion --> Context["邻句恢复 / 原文预算 / 来源白名单"]
    Context --> LLM["外部 DeepSeek 回答"]
    LLM --> Cited["实际引用筛选 / 保存聊天与引用编号"]
    Cited --> SQL

    API --> Export["GET /knowledge/export/{id}"]
    SQL --> Export
    Export --> Original["original: 完整原文与时间，本地格式化"]
    Export --> Notes["ai: 外部 LLM 整理并附完整原文"]
~~~

### 2.1 主要入口与源码证据

下列路径以仓库根为基准；行号对应审查基线。新增 probe 没有改变这些文件的行号。

| 模块 | 入口 / 关键位置 | 实际职责与边界 |
| --- | --- | --- |
| 启动 | backend/app/main.py:23、:73；backend/app/api/router.py:10 | lifespan 建目录、建表、轻量迁移、启动 worker；FastAPI 应用入口。无桌面安装器。 |
| 登录 / 采集 | backend/app/api/routes/auth.py:17；backend/app/services/douyin_collector.py:96、:175 | start_login → 持久化 Chromium → 登录检查 → 同会话采集收藏 → 保存 state.json。依赖真实平台登录态。 |
| 收藏网页接口 | backend/app/services/douyin_collector.py:223–245 | 扫描 window.webpackChunkdouyin_web，寻找 collects/list 与 collects/video/list，保留固定导出名 fallback；依赖网页内部实现。 |
| 收藏保存 | backend/app/api/routes/favorites.py:19；backend/app/services/favorites_service.py:40、:254 | sync_from_douyin → 差异同步 → 新建 pending VideoCache。 |
| 入库接纳 | backend/app/api/routes/knowledge.py:61；backend/app/services/knowledge_service.py:45–147 | 校验显式选择、去重、检查 Omni 配置、归类、提交内存任务；done 不重复识别。 |
| 工作线程 | backend/app/services/worker.py:38、:98；knowledge_service.py:157 | queue.Queue / task 字典 / daemon thread；任务自己创建数据库 session。 |
| 主入库编排 | backend/app/services/knowledge_service.py:164–328 | 下载 → 转写 → 先保存完整结果 → 分块 → embedding → Chroma → done；失败保留可用识别结果。 |
| 音频下载 | backend/app/services/media_service.py:168–216 | download_audio；抖音专用下载或通用 yt-dlp；ffmpeg 转单声道 16 kHz PCM16 WAV，校验后替换缓存。 |
| 抖音媒体获取 | backend/app/services/douyin_media.py:169、:237 | 浏览器监听详情和媒体播放，要求本地登录态；下载域名与重定向检查。 |
| 本地音频处理 | backend/app/services/audio_media.py:72、:91 | probe_audio / prepare_audio_segments，接收 Path；最大三小时，默认每段 60 秒；实际使用 ffmpeg。 |
| 当前转写 | backend/app/services/omni_service.py:204、:305 | _analyze_audio_segment / analyze_audio；base64 音频发送外部 Qwen，默认 3 路并发，校验 JSON / 时间边界、失败拆分重试、按原时间顺序合并。 |
| 可选音视频 | backend/app/services/video_media.py:38、:90；omni_service.py:611 | download_video / prepare_video_segments / analyze_video；音轨与无声画面分开分析；视频上限两小时。不是默认音频路径。 |
| 正式分块 | backend/app/services/omni_service.py:95–110；text_processing.py:94 | VideoAnalysis.knowledge_chunks → build_fixed_chunks；默认 1000 字符、200 重叠，时间戳先写入文本。 |
| 留存旧模块 | backend/app/services/asr_service.py:26；text_processing.py:128 | Paraformer ASR → RawSegment / build_chunks_from_segments → ChunkResult；当前正式入库未调用。 |
| 向量化 | backend/app/services/llm_service.py:219–251 | EmbeddingClient.embed_texts；外部 DashScope 兼容接口，最多十段一批，校验条数、index 与有限向量数值。 |
| 索引 | backend/app/services/chroma_service.py:33、:60、:111、:196 | PersistentClient / upsert_video_chunks / search / get_documents；本地向量与全文块存储。 |
| RAG | backend/app/services/rag_service.py:225、:723、:891 | answer / answer_stream；direct、db_list、db_content、vector 分流；scope、查询向量、融合、原文上下文、答案生成。 |
| 检索上下文 | backend/app/services/retrieval_fusion.py:106；retrieval_context.py | rerank_chunks / expand_hit_context / select_context_hits；排除纯标题、同来源邻句恢复、8 块 / 8000 字符默认预算。 |
| 引用 | backend/app/services/rag_service.py:647、:681、:705、:1110 | _source_candidates / _cited_sources / _ensure_sources；仅回传实际标注且处于白名单的来源。历史只存引用编号，不补造缺失的完整 evidence trace。 |
| Markdown | backend/app/api/routes/knowledge.py:187；backend/app/services/markdown_export.py:42、:61、:87 | _original_sections / export_original / export_ai_organized；原文格式化无需模型，整理版会调用 LLM，长文本分段整理再汇总并附原文。 |

### 2.2 数据模型与存储

模型均在 backend/app/models/entities.py：

| 模型 | 行号 | 关键字段 |
| --- | --- | --- |
| FavoriteCollection | 19–61 | platform_collection_id 唯一、title、video_count、active、时间 |
| FavoriteVideo | 64–120 | collection_id、platform_item_id 全局唯一、title、author、duration、video_url、cover_url |
| VideoCache | 123–192 | platform_item_id 唯一、完整 transcript_text、visual_analysis_text、analysis_json、model、duration、status、error、processed_at；summary 只是预留 |
| KnowledgeBase | 195–210 | name、规范化 name_key 唯一 |
| KnowledgeBaseVideo | 213–229 | knowledge_base_id + platform_item_id 联合主键；多知识库共用同一份识别结果 |
| ChatSession | 232–268 | title、knowledge_base_id、时间 |
| ChatMessage | 271–332 | role、content、route_type、retrieved_video_ids、retrieved_chunk_ids、model、latency |

analysis_json 保存 schema_version、analysis_mode、model、duration_seconds、transcript_segments（秒级数值）、visual_segments、warnings、raw_results、audio_processing，以及可选补听结果。TimedText schema 与 OmniService._validate_audio_times 的时间范围校验不等于逐字准确性验证；VideoAnalysis 本身是普通 dataclass，不负责该校验。

从 backend 启动时的默认位置：

| 数据 | 默认位置 |
| --- | --- |
| SQLite | backend/app/storage/douyinrag.db |
| Chroma | backend/app/storage/chroma/；collection 为 douyinrag_videos |
| 音频 / 视频缓存 | backend/app/storage/audio_cache/、video_cache/ |
| checkpoint | 媒体旁 *.analysis.json / *.boundary-analysis.json |
| 入库计时 | backend/app/storage/ingestion_runs/<video_id>/<uuid>.json |
| 平台登录态 | backend/app/storage/playwright_user_data/，包括 state.json |
| 浏览器二进制 | backend/app/storage/playwright_browsers/ |
| 应用 / 启动日志 | backend/logs/、backend/app/storage/local_services/ |
| Markdown | API 返回下载内容；无独立服务端笔记实体或默认持久笔记库 |

配置来源：backend/app/core/config.py。相对目录依赖启动 cwd，桌面集成时需集中到明确的应用数据根目录。现有 .gitignore 忽略 storage、日志、env、数据库、Cookie、浏览器状态，但并不自动覆盖用户任意指定位置的所有导出 Markdown。

### 2.3 外部依赖与“已经实现”的准确范围

| 能力 | 实现状态 | 本次能证明什么 / 不能证明什么 |
| --- | --- | --- |
| 时间段结果、分块、SQLite、Chroma、原文导出 | 已实现 | 本次真实本地保存/查询/导出通过；识别输入和向量为合成 |
| 音频规范化、分段、尾段覆盖 | 已实现 | 原套件中真实 ffmpeg + 合成媒体测试通过；不是用户视频识别 |
| 抖音收藏登录、发现与下载 | 有实现 | 依赖平台账号与网页内部接口；本次未安装/运行登录浏览器，不验证线上可用性 |
| Qwen Omni 转写、可选画面分析 | 有实现 | 需要 qwen_omni_api_key 或 dashscope_api_key；本次没有真实请求或识别质量证据 |
| DashScope embedding | 有实现 | 需要 dashscope_api_key；本次使用 mock 向量，不能判断语义质量 |
| DeepSeek 问答与 AI 整理 | 有实现 | 需要 deepseek_api_key；mock 只验证编排/格式，不能判断回答或笔记质量 |
| 纯本地无密钥语音识别 / AI 笔记 | 未实现 | 没有已集成的本地 ASR 或本地 LLM |
| 全文搜索 | 有 RAG 词法检索基础 | 范围内读取 Chroma 原文、内存词法融合；没有独立 FTS API/UI |
| 单链接、本地导入、博主、YouTube/Bilibili | 无应用导入入口 | 下载函数有通用 yt-dlp 分支，不能等同平台接入完成 |
| 人工笔记、可恢复全库导出、桌面安装、AI Memory Lab | 未实现 | 属于下一阶段或更远规划 |

外部地址为配置值：Qwen/embedding 默认 DashScope compatible-mode，回答默认 api.deepseek.com。音频、索引文本、问题与选用原文会发送给对应服务；本地数据库不意味着全流程不出机。README/.env.example 的 deepseek-flash 与 Settings 无 env 时的 deepseek-chat 需区分。本次没有核验模型名或账号区域的可用性。

## 3. 环境、运行命令与完整测试结果

### 3.1 安装环境

Windows / PowerShell，Python 3.12.14、uv 0.13.5、Node v24.13.1、npm 11.8.0、Git 2.53.0.windows.1。初始 PATH 没有 uv / ffmpeg；使用应用已有 Python，工具安装在仓库外 D:\新建文件夹\.phase0-tools，未修改系统 PATH。ffmpeg 7.1 来自 imageio-ffmpeg 0.6.0 Windows wheel，仅用于本地测试；该二进制构建启用了 GPL，未随仓库分发。

上游没有 backend/uv.lock，pyproject.toml 主要采用 >= 下限。uv 本次解析 111 个包并安装 108 个；生成的 backend/uv.lock 作为这次环境快照保留。它不是上游原有锁定或跨操作系统兼容保证。frontend/package-lock.json 未改。

关键实际版本：FastAPI 0.159.1、SQLAlchemy 2.1.6、ChromaDB 1.5.9、DashScope 2.0.1、OpenAI SDK 3.36.1、httpx 0.28.1、httpx2 2.16.0、Pydantic 2.14.1、pytest 9.1.1、pytest-asyncio 1.4.0、yt-dlp 2026.10.21。完整 freeze 保存在本地证据目录。

### 3.2 实际命令

Fork 操作：

~~~powershell
cd 'D:\新建文件夹'
gh repo view baigneuse/douyinmind --json nameWithOwner,isFork,parent,url,defaultBranchRef
# 初查不存在，然后创建：
gh repo fork MQTQW/douyinmind --clone
cd douyinmind
git rev-parse HEAD
git remote -v
~~~

初次尝试的 gh repo fork MQTQW/douyinmind --clone --remote 因 CLI 不允许“仓库参数与 --remote 同用”退出；去掉不支持的 --remote 后成功，没有重复创建 Fork。

工具与依赖安装（本机路径）：

~~~powershell
$phase0Python = 'C:\Users\51264\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$phase0Tools = 'D:\新建文件夹\.phase0-tools'
& $phase0Python -m pip install --target $phase0Tools uv
& $phase0Python -m pip install --target $phase0Tools imageio-ffmpeg
Copy-Item -LiteralPath "$phase0Tools\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe" -Destination "$phase0Tools\bin\ffmpeg.exe"

cd 'D:\新建文件夹\douyinmind\backend'
& "$phase0Tools\bin\uv.exe" sync --python $phase0Python --extra dev
# 等价于已有 Python 3.12 可发现时：
# uv sync --python 3.12 --extra dev

cd '..\frontend'
npm.cmd ci
npm.cmd test
npm.cmd run build
npm.cmd audit --json
~~~

使用 npm.cmd 避开 Windows PowerShell 脚本执行策略。安装中的跨盘 hardlink 警告自动回退 copy，安装成功。

后端测试运行前，清空 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / QWEN_OMNI_API_KEY / OPENAI_API_KEY；没有创建 backend/.env。设置 ANONYMIZED_TELEMETRY=False，并将数据库、缓存与浏览器状态目录指向仓库外空测试目录。ffmpeg 只加入测试进程 PATH。原套件没有施加 OS 级网络隔离；其 mock 测试不应被表述为真实服务验证。

~~~powershell
cd 'D:\新建文件夹\douyinmind\backend'
$env:PATH = 'D:\新建文件夹\.phase0-tools\bin;' + $env:PATH
$env:PYTHONIOENCODING = 'utf-8'
$env:DEEPSEEK_API_KEY = ''
$env:DASHSCOPE_API_KEY = ''
$env:QWEN_OMNI_API_KEY = ''
$env:OPENAI_API_KEY = ''
$env:ANONYMIZED_TELEMETRY = 'False'
# 首轮：默认 pytest temp root
uv run python -m pytest -ra --junitxml='D:/新建文件夹/phase0-evidence/backend-junit.xml'
# 第二轮：仅为 tmp_path 指定新的本地目录
uv run python -m pytest -ra --basetemp='D:/新建文件夹/phase0-evidence/pytest-temp-20261022-r2' --junitxml='D:/新建文件夹/phase0-evidence/backend-junit-r2.xml'
~~~

本机实际调用 uv 的绝对路径为 D:\新建文件夹\.phase0-tools\bin\uv.exe。含存储路径环境变量的完整第二轮命令保存为 D:\新建文件夹\phase0-evidence\run_backend_tests.ps1。再次运行时应选一个全新 basetemp，不覆盖此前证据；pytest 会管理该目录内容。

### 3.3 结果与失败原因

| 检查 | 通过 | 失败 / error | 跳过 | 退出码 / 备注 |
| --- | ---: | --- | ---: | --- |
| backend uv sync | 安装成功 | 0 | — | 0；108 包安装 |
| frontend npm ci | 安装成功 | 0 | — | 0；234 包新增，audit 235 包 |
| frontend npm test | 72 | 0 | 0 | 0；27.43 秒 |
| frontend npm run build | 成功 | 0 | — | 0；tsc -b + Vite，2212 modules，Vite 构建 54.55 秒 |
| backend pytest 首轮 | 0 | 880 setup errors，0 assertion failures | 0 | 1；67.86 秒；所有测试主体均未运行 |
| backend pytest 第二轮 | 879 | 0 failures / 0 errors | 1 | 0；69.64 秒 |
| 真实 HTTP 启动 smoke | 3 个 GET 检查成功 | 最终 0 | — | 最终 0；空库、临时存储 |
| 离线集成 probe | 1 条流程 | 最终 0 | 0 | 0；合成数据，真实 SQL / Chroma |
| 两个额外缺陷 probe | 两项均复现 | 2 项产品缺陷 | — | 探针 exit 0 表示复现成立，不是产品正确 |
| npm audit | — | 8 项漏洞报告 | — | 1；不等同 build/test 失败 |

首轮共同错误：

~~~text
PermissionError: [WinError 5] 拒绝访问。:
C:\Users\51264\AppData\Local\Temp\pytest-of-51264
~~~

autouse fixture isolated_ingestion_metrics 依赖 tmp_path，因此所有 880 项都在 setup 被同一环境权限问题阻挡。只设置 --basetemp 重跑，不修改该旧目录 ACL、不删除别人的测试数据、不修改断言。

唯一既有 skip：

- 测试：tests/test_audio_boundary_independent.py::test_frozen_real_four_windows_keep_outer_original_not_whole_window。
- 位置：该文件第 148 行。
- 原因：Local frozen real experiment not present。
- 所需本地输入：backend/app/storage/continuous_testing/2026-10-19/round-05/audio 下四个交界点 120/240/420/720 的 A-left、A-right、B-continuous result JSON。
- 这些未公开真实实验数据不在仓库，未伪造，也没有为通过而取消 skip。其他 ffmpeg 条件测试在工具可用后真实执行。

成功的媒体测试包括 test_audio_only.py 的真实 WAV 尾段覆盖、test_video_segments.py 的合成视频分段与分离音轨，以及 test_douyin_media.py 中合成媒体完整性检查。不能从这些结果推导真实下载、真实 ASR 或用户媒体质量成功。

npm audit 的包：high 为 braces、chokidar、fast-glob、micromatch、tailwindcss、vite；moderate 为 postcss-nested、postcss-selector-parser。直接依赖包括 tailwindcss、vite。报告来源为本次 npm registry 审计返回；未逐条做可利用性验证，未执行 audit fix 或锁文件升级。

### 3.4 运行服务验证

用空临时 cwd、隔离环境和临时数据库启动实际进程：

~~~powershell
.\.venv\Scripts\python.exe 'D:\新建文件夹\phase0-evidence\startup_smoke.py' 'D:\新建文件夹\douyinmind\backend'
# 脚本内部等价启动：
# python -m uvicorn app.main:app --app-dir <backend> --host 127.0.0.1 --port <空闲本地端口>
~~~

GET / 返回 status=ok、service=DouyinRAG、version=0.1.0；GET /openapi.json 包含 25 条 paths；GET /api/knowledge/stats 返回空 SQLite / Chroma 计数。未调用登录、导入、模型或用户资料路由。服务器已停止，成功重跑的临时目录已清除。没有浏览器 UI 验收，前端证据限于自动化测试和构建。

首次 smoke 的三个 HTTP 检查已通过，但临时 Chroma 文件清理遇到 WinError 32（文件仍被占用），脚本因此 exit 1；给验证脚本的临时目录清理增加短暂重试后，再次运行 exit 0。未修改应用关闭实现。第一次残留合成目录的后续递归删除被工具自动审批策略拒绝（未提供更具体原因），保留在 C:\Users\51264\AppData\Local\Temp\douyinmind-phase0-boot-ysekvsnr；没有绕过该拒绝。

## 4. 离线流程的可重复验证与停点

新增可执行脚本：[backend/phase0_offline_probe.py](../backend/phase0_offline_probe.py)。

~~~powershell
cd 'D:\新建文件夹\douyinmind\backend'
uv run python phase0_offline_probe.py
# 本机实际执行：
.\.venv\Scripts\python.exe phase0_offline_probe.py
~~~

复用 tests/test_omni_ingestion.py::saved_result()。为验证默认 audio 路径，保留 fixture 原始 transcript_text / transcript_segments，清空视觉结果，并明确把模型字段标成 synthetic。不是新生成的真实转写。

| 阶段 | 实际执行 | mock / 证据边界 |
| --- | --- | --- |
| 输入 | 1 段合成文字，0–5 秒 | fixture，不是真实音视频 |
| 下载 | 正式编排调用 download_audio | 替换为只写占位字节的 mock，没有网络或 ffmpeg |
| 识别 | 正式编排调用 analyze_audio | 返回合成 VideoAnalysis；视觉路径若被调用立即失败 |
| 结果保存 | 真实 SQLAlchemy / 文件 SQLite / VideoCache | 1 行 done；新 session 重读确认文字与数值时间段一致 |
| 分块 | 真实 knowledge_chunks → build_fixed_chunks | 2 块：标题 + 带 [00:00–00:05] 的语音正文 |
| 向量 | embed_texts mock 一次 | 固定 4 维非零向量，无语义含义 |
| 索引与查询 | 真实 Chroma PersistentClient、upsert、get_documents、search | 2 chunks、1 video、1 条指定向量查询命中；不是语义检索评测 |
| 原文导出 | 真实 export_original | 无模型；完整文字和时间保留 |
| 笔记导出 | 真实 export_ai_organized | llm_client.chat 固定返回一次，并标明 SYNTHETIC MOCK LLM |
| 重复处理 | 再次调用 _process_videos 处理 done ID | 不增加下载/识别/embedding 次数 |
| 旧切块器 | 独立 build_chunks_from_segments 检查 | 1 块、0..5000 ms；不是正式入库路径 |

download / analysis / embedding / LLM 各 mock 1 次。Python socket connect / connect_ex / sendto / create_connection / DNS 被阻止，保护自检通过，应用链路期间意外 Python 网络尝试为 0；这不是 OS 防火墙证明。子进程使用空环境和临时 cwd，不加载 backend/.env；临时 SQL、Chroma、占位媒体、Markdown 退出后删除。

probe 首轮曾因严格隔离去掉 USERPROFILE，导致 Chroma 的默认 embedding 类在导入时无法求 Path.home()；只将子进程 USERPROFILE / APPDATA / LOCALAPPDATA 指向临时目录后通过。这属于新增验证脚本环境问题，不算上游测试失败。

时间戳边界：

- SQLite analysis_json 保留输入的数值时间。
- 当前 index / Markdown 使用 timestamp(int(seconds)) 的秒级文本展示。
- Chroma metadata 只有 chunk_id、chunk_index、created_at、platform_item_id、title，没有结构化起止时间。
- 固定字符切块可能拆开时间标签与语句；单个短 fixture 通过不证明任意长转写切块边界完整。
- 源引用仍为 synthetic ID 拼出抖音 URL，这直接暴露来源抽象缺口。

实际无 mock 时会停在哪里：

1. 现有 start_sync 在 knowledge_service.py:111 的 omni_service.check_configuration() 就会因缺密钥被拒绝。
2. 直接进入编排还需要 download_audio 的可访问抖音登录态/有效媒体；当前没有 local import 入口。
3. analyze_audio 在 omni_service.py:306 检查配置，并依赖真实可解码音频及 Qwen 服务。
4. EmbeddingClient.embed_texts 在 llm_service.py:230–231 要求 DashScope key。
5. export_original 对已保存 fixture 可离线工作；export_ai_organized 需要 DeepSeek 配置或一个未来新增的本地 LLM adapter。

因此本阶段已经验证“合成识别结果之后的数据链路”，未验证“真实媒体 → 识别结果”与模型输出质量。真实媒体验收需用户有处理权限的输入、选定可用 ASR 后端，并核对处理的数据边界。

## 5. 来源扩展、重复导入、恢复与数据保留

### 5.1 最小来源边界

最合适的边界是：

~~~text
来源枚举/解析 → Source metadata + 应用拥有的媒体 Path
            → 规范音频 → Transcriber → VideoAnalysis
            → 先保存原始结果 → 原文/知识笔记导出
            → 可独立重试的索引（后续）
~~~

本阶段没有实现任何新来源。

| 来源 | 现状 | 最小下一步 |
| --- | --- | --- |
| 抖音单链接 | 无单链接导入 API；当前请求只接受已存在 VideoCache ID | 新增单链接解析/创建来源记录，复用后续处理；不能仅写 FavoriteVideo.video_url |
| 本地文件 | analyze_audio、probe_audio 接收 Path，但编排强制下载 | 增加薄 CLI、只读原文件、生成受控缓存副本，连接来源无关分析/导出 |
| YouTube | 下载层有 yt-dlp 通用分支 | 未来新增 provider metadata adapter、正确 source URL / ID，独立验收平台下载 |
| Bilibili | 同上 | 独立 adapter 和平台访问约束，不因 yt-dlp 在依赖中就宣称接通 |
| 指定博主 | 没有采集入口 | 未来来源枚举 adapter，不属于本地 MVP |

明确硬编码：

- knowledge_service.py:206 直接拼抖音 URL，不读取 FavoriteVideo.video_url。
- markdown_export.py:75、:138；routes/knowledge.py:50；knowledge_base_service.py:173；rag_service.py:655、:1166 均构造抖音来源链接。
- entities.py 的 platform_item_id 为全局唯一 String(64)，没有 provider namespace / source_url / local hash。
- media_service.py:175、video_media.py:39、knowledge_service.py:416 仅接受 [A-Za-z0-9_-]+；ingestion_metrics.py:18 另限 1–64 字符。不能直接塞 local:<64位hash> 或绝对路径。
- video_media.py:58–60 仍有 douyin.com 字符串分支。
- **knowledge_service.py:309–312 在成功后删除 media_path。local adapter 必须返回应用自己的缓存副本，不能返回用户原文件。**

### 5.2 去重与恢复

| 项目 | 已有机制 | 缺口 |
| --- | --- | --- |
| 重复提交 | start_sync 去重 selected_ids，仅选 pending/failed；运行时再次复用 done | 按平台 ID，不按文件内容；模型/提示更新不会自动定义重跑版本 |
| 多知识库 | KnowledgeBaseVideo 幂等关联、共享 VideoCache | 不能据此推导跨平台内容去重 |
| 缓存复用 | 完整 WAV 校验后复用 | 缓存 key 仍是平台 ID |
| 识别恢复 | audio_checkpoint.py:26–44 内容 SHA256 + 配置指纹；:134–145 原子写；Omni 重用成功 root/child | 这是片段级恢复，不是持久任务队列 |
| 服务重启 | /knowledge/reset-failed 将失败/处理中重设 pending，再手动导入 | queue、进度与 task registry 在内存；无启动自动恢复、多进程统一互斥 |
| 索引失败 | 先 commit 原识别结果，避免识别成功却丢失原文 | 重试仍走总编排，缺少“只重建索引”入口 |
| 双写一致性 | SQLite / Chroma 都可本地保存 | Chroma 先删旧再 upsert；不与 SQLite 原子提交，替换失败可丢旧索引 |
| 取消任务 | Worker 注释声称支持取消 | 未找到 cancel 实现或 API，应视为未实现 |

### 5.3 已动态复现的额外缺陷

这些检查使用现有 tests/test_knowledge_bases.py 的内存 SQLite fixture、真实 Ephemeral Chroma 和显式合成向量。未调用模型或采集服务。允许 Windows asyncio 所需数字 loopback 连接，外部连接被拦；外部尝试为 0。

复现命令：

~~~powershell
cd 'D:\新建文件夹\phase0-evidence'
& 'D:\新建文件夹\douyinmind\backend\.venv\Scripts\python.exe' '.\risk_reproductions.py'
~~~

| 缺陷 | 输入与观察 | 根因 / 限制 |
| --- | --- | --- |
| 同视频在两个收藏夹同步失败 | 两个收藏夹、一个视频有两条 membership；抛 IntegrityError: UNIQUE constraint failed: favorite_videos.platform_item_id | favorites_service.py:171–184、:217–230 按 (collection,id) 创建；entities.py:81–83 却让 id 单列全局唯一 |
| 未归类资料取消收藏留下孤儿索引 | 两个 done 视频都入 Chroma，其中一个归命名库；空收藏同步后，未归类 VideoCache 被删、pending:0 chunk 残留；命名库原文保留 | favorites_service.py:269–290 的孤儿缓存删除没有对应 Chroma 清理；只验证了索引残留，未证明它会进入最终 RAG 答案 |

探针首次曾过度封锁 socket，误拦 Windows asyncio 内部 socketpair；允许数字 loopback 后完成，初次记录也保留。探针退出 0 表示缺陷复现成功，不能写成产品测试通过。这两项在 Phase 0 保留原状，未为消除失败大改实现。

此外，collector.fetch_snapshot（douyin_collector.py:347–353）只返回登录时的内存快照；再次点“同步”不会重新采集网页，属于静态确认的行为。使用说明“取消收藏不会清空已有知识库资料”实际保护命名知识库成员，不能扩展理解成所有未归类资料永远保留。

### 5.4 人工笔记和导出

- 没有 UserNote、编辑 API、笔记版本或生成稿保留策略；summary 字段只是预留。
- 再识别会覆盖 transcript_text / analysis_json，不能把人工修订混写这里。
- 原文 Markdown 包含全文、时间、画面和 warnings；AI 整理每次即时请求 LLM，不持久保存结果。
- 没有统一 JSON/SRT/VTT 导出、全库 portable bundle、导入恢复或版本化 manifest。
- 下阶段至少区分原始 transcript.json、generated-notes.md 与人工 notes.md；重跑不覆盖人工文件。索引应可由原文和来源 metadata 重建。

## 6. 许可证与复用边界

执行 git ls-files / git ls-tree 搜索 LICENSE、license、COPYING、NOTICE，并核对 README 和两个依赖 manifest：

- README.md:205–207 仅写 License / MIT。
- 审查 commit 的 tracked tree **没有独立 LICENSE / COPYING / NOTICE**。
- backend/pyproject.toml 和 frontend/package.json 未提供 license 字段。
- 这是“声明存在，但完整许可文本/版权信息未随仓库核实”的事实记录；不据此作完整法律授权结论。
- 遵守用户边界：只在 Fork 中审查、保留来源历史，不把源码迁入新的独立产品，不代上游作者补写授权文件。
- 独立产品复用/分发前，需上游补齐或明确授权，并核查依赖、ffmpeg 和将来本地模型权重的分发条件。本次仅验证运行，不分发二进制。

## 7. 主要技术风险与保留价值

| 优先级 | 风险 | 证据 / 下一阶段动作 |
| --- | --- | --- |
| 高 | 复用授权文本不完整 | 源码树检查；独立产品抽取前解决 |
| 高 | 本地原文件可能被既有清理流程删除 | 明确源码路径；所有媒体处理只使用受控副本 |
| 高 | 跨收藏夹同视频同步唯一冲突 | 离线动态复现；后续针对关联唯一性做小修复并加回归 |
| 高 | 取消收藏后数据库/索引分离 | 动态复现；明确资产保留政策和索引清理/重建 |
| 高 | 抖音 URL 与身份硬编码 | 主编排及导出/引用路径确认；集中 Source 元数据 |
| 中高 | 内存任务与跨库非原子 | 源码确认；最小 CLI 用阶段 manifest，后续再做持久队列 |
| 中高 | 模型质量和真实平台链路未验证 | 不用 mock 替代真实验收；下一阶段用授权短媒体回听 |
| 中 | 时间只在 chunk 文本中，字符切块可拆段 | probe + 源码；保留 segment ID / start / end 元数据 |
| 中 | 没有人工笔记保护 | 分离用户内容与生成内容，版本化输出 |
| 中 | 前端依赖审计 8 项风险 | 本次 npm audit；另行小范围升级并回归，未自动修复 |
| 中 | 后端只有版本下限、API 默认/模板差异 | 保留本次锁快照；未来升级需要回归 |
| 中 | 词法检索每次遍历范围原文 | Chroma get_documents 每批 500、内存计算；大规模 FTS 需另设计 |
| 中 | 平台网页内部 API / Cookie 依赖 | 不稳定采集边界；collector 保持专用 adapter |

值得保留：audio_media / audio_checkpoint；Omni 的时间校验、片段复用及 VideoAnalysis 契约；识别结果先于索引保存的顺序；原文导出与整理版附原文结构；多知识库归类；RAG 的 scope、原文预算、邻句恢复、真实引用白名单、错误分级与既有回归测试。

不宜直接作为新产品核心接口：抖音收藏快照、platform_item_id、硬编码 URL、全局配置单例、纯内存 worker。独立全文搜索、人工笔记、桌面分发等还不能算已实现。

## 8. 继续 Fork 与另建项目

| 选择 | 优势 | 成本与前提 |
| --- | --- | --- |
| 继续 Fork（建议） | 保留现有处理/恢复/证据链和 880 个后端、72 个前端测试；小范围验证本地导入；上游更新易对照 | 逐步消除抖音假设、修复保留/一致性缺陷；保留 legacy 与当前 Omni 的区分；后续分发授权仍需核实 |
| 抽取模块另建 | 可重新设计统一 Source / Asset / Transcript / Note 和桌面边界 | 当前许可证文本缺失；要重新组织测试、恢复、存储迁移与打包；很容易扩大 Phase 0 范围 |

先用一个 CLI 证明用户价值，再以真实输入、恢复与输出质量证据决定是否抽取。没有依据在此阶段重写 UI 或增加完整框架。

## 9. 下一阶段：一段本地音视频 → 逐字稿 + 知识笔记

此节是具体实施方案，**尚未实施**。

建议验收目标：输入一段用户拥有处理权限的短音频或视频；原文件保持不变；输出完整带起止时间的 transcript.json、transcript.md，以及由真实模型生成、可回查时间段的 generated-notes.md。模板或 mock 不算知识提炼成功。

最小实现清单：

1. **增加薄 CLI 入口** backend/scripts/import_local.py，并仅增加该文件的 .gitignore 白名单。参数 --input、--output-dir、--title、--transcriber；不重写网页。检查路径、文件大小、音轨和时长，原文件只读。
2. **抽出规范化小函数** normalize_audio(source, owned_cache)，复用 ffmpeg 参数、完整 WAV 校验和 prepare_audio_segments。只清理应用拥有的副本；原文件 SHA256 在前后都保持一致。
3. **增加来源 manifest，而非先迁移整个系统。** 保存内部安全 ID、source_type=local、original_name、content_sha256、duration、处理版本、各阶段状态。绝对路径只留本地；导出不伪造抖音 URL。正式接入知识库时再迁移统一来源字段和引用生成。
4. **保留来源无关的转写结果边界。** 复用 VideoAnalysis / transcript_segments / warnings / raw_results。最少代码路径是复用 OmniService.analyze_audio，但下一阶段必须由用户明确配置并允许向外部服务发送该媒体；本阶段绝不调用。若下一阶段继续要求无密钥、不上传，则需要新增本地 ASR adapter 映射到同一结果契约；当前不存在，需另验模型下载、硬件、中文长音频与时间戳质量。
5. **先可靠输出逐字稿，再生成笔记。** 原始 JSON、逐字稿 Markdown 先原子落盘。给现有 Markdown helper 增加 source metadata 参数，复用来源、逐段时间、warnings。真实知识笔记复用现有 LLM 整理逻辑或新增已选定的本地 LLM adapter，按“摘要、主题、关键论点、步骤、行动项、未明确事项、时间引用”输出，不能只写空模板便标完成。
6. **首个切片不强制 embedding。** 输出任务与建索引解耦；笔记失败保留逐字稿，索引失败不阻止用户获得文件。之后才接 Chroma、全文搜索、跨视频问答。
7. **最小恢复与人工内容保护。** 内容散列 + 处理配置指纹复用同一任务；manifest 区分 normalized / transcribed / notes_done。复用当前识别 checkpoint，但不把它当完整任务持久化。每次生成记录版本；notes.md 仅首次建立、绝不自动覆盖。
8. **输出 manifest 为未来集成接口。** 先稳定资产 ID、来源信息、逐段文本/起止时间、处理版本与输出路径；AI Memory Lab / Electron / Tauri 未来消费该接口，本次不接其数据库或基础设施。

建议输出（仅本地，Git 忽略）：

~~~text
app/storage/local_imports/<asset_id>/
  manifest.json
  transcript.json
  transcript.md
  generated-notes.<revision>.md
  notes.md
  cache/normalized.wav
~~~

真实验收步骤：

- 一段用户有权处理的 30–120 秒音视频，含可核对开头、中段、结尾，确认字句、尾句与时间对应；记录识别错误，不把格式通过当准确率。
- 相同输入重复执行不重复转写；改变处理配置产生新 revision。
- 在转写成功后制造笔记阶段失败，确认逐字稿仍可导出；重跑只执行缺失阶段。
- 人工修改 notes.md 后重跑，文件内容保持不变；原始媒体散列不变。
- 无人声、损坏媒体、超时/配置错误都明确失败，不生成伪造逐字稿。
- 若采用外部模型，记录真实服务、模型、请求次数和处理边界；若采用本地模型，记录实际运行硬件、模型版本与耗时。两种均需真实媒体证据，不复用 Phase 0 mock 作为验收。

## 10. 证据位置与交付检查

本地原始证据统一位于 D:\新建文件夹\phase0-evidence，未上传：

| 文件 | 内容 |
| --- | --- |
| backend-sync.log / backend-freeze.txt | 后端安装日志与实际版本 |
| backend-test.log / backend-junit.xml | 首轮 880 环境 setup errors |
| backend-test-r2.log / backend-junit-r2.xml | 879 pass、1 原有 skip，逐项详情 |
| run_backend_tests.ps1 | 第二轮完整隔离环境与命令 |
| frontend-test.log / frontend-build.log | 72 pass、构建输出 |
| frontend-audit.json | npm 漏洞原始报告 |
| offline-probe.log | 正式离线集成 JSON 输出 |
| risk_reproductions.py / .json / .log | 两项额外缺陷的可执行复现与观察 |
| risk_reproductions_initial_attempt.txt | 风险探针首次隔离设置失败 |
| startup_smoke.py / startup-smoke.log / startup-smoke-r2.log | 实际 HTTP 启动、首次清理错误及成功重跑 |
| startup-server.log | 最近一次 Uvicorn 启动和三个 GET 200 |

仓库内交付：docs/PHASE0_AUDIT.md、backend/phase0_offline_probe.py、安装生成的 backend/uv.lock。既有实现、测试与 frontend/package-lock.json 保持原样；新增文件经过空白差异检查与凭证形态检查。没有个人视频、真实逐字稿、Cookie、真实密钥或生产配置进入交付。
