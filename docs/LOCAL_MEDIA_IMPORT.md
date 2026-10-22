# 本地音视频导入 CLI

Phase 1 在现有后端提供薄 CLI：读取本地媒体及已有字幕，输出带毫秒时间戳的逐字稿和 Markdown 文件。默认完全离线；有凭证并逐次明确允许后，才可启用 Qwen Omni 转写或 DeepSeek 知识笔记。此入口不登录抖音，不下载本地 ASR 模型，也不启动前端。

## 准备与最短命令

需要 Python 3.12、仓库后端依赖和可执行的 ffmpeg。使用已有环境即可；新环境在仓库根目录执行：

```powershell
cd backend
uv sync --python 3.12 --extra dev
uv run python -m scripts.import_local --help
```

ffmpeg 优先从当前进程 `PATH` 查找，也支持现有 `backend/app/storage/tools/ffmpeg.exe`。CLI 不自动下载安装 ffmpeg。若已在机器上准备好，可把其 `bin` 目录加入本次 PowerShell 的 `PATH`；无需改动全局系统变量。

准备一份你有权处理的媒体和对应 SRT/VTT，以带引号的路径运行：

```powershell
uv run python -m scripts.import_local --input 'D:\素材\访谈_日本語.mp4' --subtitles 'D:\素材\访谈_日本語.vtt'
```

这等价于 `--mode subtitles --summary none`。已有 `.venv` 时也可用 `.\.venv\Scripts\python.exe -m scripts.import_local ...`，避免 `uv run` 在缺依赖时尝试环境同步。进度写到 stderr，最终结果 JSON 写到 stdout；命令退出后检查 `status` 与 `summary_status`，不能仅凭退出码 0 判断 AI 总结已经生成。

## 输入边界

| 项目 | 当前限制 |
| --- | --- |
| 媒体 | MP4、MP3、M4A、WAV；可读的非空文件，最大 2 GiB，时长大于 0 且不超过 3 小时；必须含可解码音轨 |
| 字幕 | SRT/VTT，非空且不超过 10 MiB；UTF-8（可带 BOM）或带 BOM 的 UTF-16 |
| 时间 | 每段为整数 `start_ms` / `end_ms`；开始不倒序，`0 ≤ start_ms < end_ms ≤ 媒体时长`；允许字幕段重叠 |
| WebVTT | 支持 cue ID、基础样式标签、说话人标签及布局设置；布局转换为纯文本；`X-TIMESTAMP-MAP` 时钟映射不支持，请提供相对本媒体的时间 |
| 文件路径 | 支持 Windows 中文、日文和空格；输入不得放在该媒体自己的结果目录中 |
| 额外资源 | 需要磁盘空间保存受控媒体副本和 16 kHz 单声道 PCM WAV；较长或损坏媒体可能触发 ffmpeg 超时 |

改扩展名不会把任意内容变成合法媒体。CLI 复制原文件到应用控制的临时目录，使用固定 demuxer 和仅本地文件协议解码，处理完成后校验原文件 SHA-256。字幕解析也不会写回输入。错误、失败或取消均不应删除原文件；若文件在运行时被其他程序改动，导入会失败并记录完整性检查结果。

已有字幕由来源负责准确性和覆盖率。本工具保留字幕文本及时间信息，不能保证字幕覆盖所有语音，也不会把字幕中的音效描述当作本次 ASR 识别。

## 输出与状态

默认输出根目录为 `backend/app/storage/local_imports/`，每份媒体按完整 SHA-256 放入 `local-media-<sha256>/`。可用 `--output-dir 'D:\本地知识库'` 指定其他输出根目录，或用 `--title '访谈标题'` 指定 manifest 标题。

| 文件 | 内容及维护方式 |
| --- | --- |
| `source_manifest.json` | 本地来源类型、文件名/散列/大小/时长、流水线与模型配置、阶段状态、输出散列、云端授权标记、原文件前后完整性 |
| `transcript.json` | `origin`、模型标识、所有片段 ID、`start_ms`、`end_ms`、完整文本和来源警告；是结构化原文结果 |
| `transcript.md` | 全部片段，含完整时间和可引用锚点；内容中的 HTML/Markdown 特殊字符进行安全转义 |
| `knowledge.md` | 摘要、主题、关键论点、重要数字及原文时间链接；未启用/未完成模型时明确写“未生成 AI 总结” |
| `notes.md` | 人工笔记，仅第一次创建；重跑、缓存失效和模型改变都不覆盖已有文件 |

`cache/` 保存规范音频和可恢复处理缓存，Qwen 转写复用原有 audio checkpoint；`.import.lock` 用于避免同一结果目录被两个任务同时写入。这些都是同一应用存储区的文件结果，没有新增数据库、SQLite 表或 Chroma collection。本阶段尚未把本地导入接入现有 UI、全文检索或 RAG。

主要状态：

| 状态 | 含义 |
| --- | --- |
| `transcript_complete_summary_not_generated` | 原文与导出完成，但 AI 总结未生成；离线默认路径的正常结果 |
| `completed` | 原文和所请求的总结均完成；仍需结合 `origin` 与模型 provider 判断真实服务还是 Mock，并人工复核模型内容 |
| `blocked` | 缺少本次云端授权或可用格式的凭证；不应理解为媒体已上传 |
| `failed` | 某阶段失败，查看 manifest 的 `stages` 与 `error`；之前保存的结果不等于本次已成功 |
| `cancelled` | 已处理取消，已有成功结果和 checkpoint 保留，可用同一配置重试 |

CLI 退出码：`0` 为本次所请求流程完成（允许总结未请求），`1` 为处理错误，`2` 为输入/配置错误，`3` 为云端条件 `BLOCKED`，`130` 为用户取消。进入输出目录创建之前的输入错误可能没有 manifest。

`knowledge.md` 将模型内容区分为“原文陈述（未经外部核验）”“作者观点”“未经核实的外部事实”。来源引用合法性校验只验证引用确实指向输入片段，不能证明模型概括或外部事实正确。没有总结模型时，文档保留空章节及明确状态；可能列出的含阿拉伯数字片段只是确定性原文摘录，不是 AI 总结。

## 云端 ASR：只有明确允许才发送音频

默认不会读取本地 `.env`；CLI 在导入应用模块前关闭 dotenv，并去除未获本次授权服务的进程凭证。Qwen 仅在 `--mode qwen --allow-cloud-asr` 后读取进程环境里的 `QWEN_OMNI_API_KEY`，未设置时回退到 `DASHSCOPE_API_KEY`。传入授权参数表示同意向指定服务发送规范化的音频片段及可能发生的费用；不会通过自动充值或自动开通服务来补足凭证。

下面是 **PowerShell 7.1+** 的交互示例；`Read-Host -MaskInput` 隐藏键入内容，命令本身不包含真实 Key，也不把 Key 写入文件。凭证在调用 SDK 时仍需以进程内字符串使用，不是凭证保险箱。

```powershell
$env:QWEN_OMNI_API_KEY = Read-Host '输入本次授权使用的 Qwen / DashScope API Key' -MaskInput
try {
    uv run python -m scripts.import_local --input 'D:\素材\访谈_日本語.mp4' --mode qwen --allow-cloud-asr --summary none
} finally {
    Remove-Item Env:QWEN_OMNI_API_KEY -ErrorAction SilentlyContinue
}
```

当前默认 `--asr-model qwen3.8-omni-flash`，`--asr-base-url https://dashscope.aliyuncs.com/compatible-mode/v1`。可通过同名参数显式更改为你的账户支持的模型和端点；端点必须是不含用户名、密码、query 或 fragment 的 HTTPS URL。模型名称来自当前仓库契约，本阶段没有用真实凭证证明账户可用性。

分段默认 `--segment-seconds 60`、并发默认 `--concurrency 3`；范围分别为 5–120 秒、1–4。本地检查可以拦截空值与明显占位凭证，但不能离线确认凭证是否真实有效；服务认证失败会记录失败，不会伪装成识别成功。使用 Mock 的测试入口不可通过 CLI 选择，测试注入结果固定标记 `mock`。

## 云端知识笔记：单独授权上传逐字稿

已有字幕也可单独请求总结，无需启用 ASR：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host '输入本次授权使用的 DeepSeek API Key' -MaskInput
try {
    uv run python -m scripts.import_local --input 'D:\素材\访谈_日本語.mp4' --subtitles 'D:\素材\访谈_日本語.vtt' --summary deepseek --allow-cloud-summary
} finally {
    Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
}
```

默认模型为 `deepseek-chat`，端点为 `https://api.deepseek.com`；分别由 `--summary-model`、`--summary-base-url` 调整。`--allow-cloud-summary` 只授权文本总结，不授权 ASR 音频上传。要把真实 ASR 与总结连起来，须同时选择 `--mode qwen --summary deepseek`，配置两组凭证并显式传入两个授权参数。

总结成功需返回受验证的结构化内容及本批原文 ID；无效 JSON、未知引用、空摘要或未完成响应都会失败。转写成功后先落盘，因此总结失败仍保留逐字稿及 checkpoint，之后修正配置并使用同样命令重试即可。本次交付没有真实云端 ASR 或真实 DeepSeek 总结验收；无凭证路径是 `BLOCKED`。

## 重跑、取消与人工笔记

同一文件内容产生相同结果目录；改名或换目录不会改变 SHA-256 身份。相同输出根目录、相同媒体和相同处理配置会校验并复用已有规范音频、逐字稿和知识笔记。字幕内容、模型、端点、提示词、解析器/流水线版本等变化会使相应缓存失效；改变总结配置不必重新识别已有效的原文。输出文件被改动或损坏后，校验失败的生成结果会重建，因此应把人工修改写入 `notes.md`。

这是当前结果及恢复缓存，不是多版本文档历史。切换模式或配置可能更新生成文件；旧文件是否可用以当前 manifest 的阶段、配置与散列为准。`notes.md` 永不因重跑覆盖。删除整个结果目录会失去人工笔记与恢复缓存，应先自行备份。

按 Ctrl+C 可取消；原文件只读，已经写入的成功逐字稿和 checkpoint 会保留。但取消无法撤回已发送给云服务的请求，也不能保证免除其费用；已有并发请求可能需要等待结束。突然断电或强制杀进程可能留下 manifest 的 `running` 状态，重新运行时会重新校验缓存；不要仅凭上一次状态文字推断仍有任务运行。

默认存储目录、`local-media-*` 结果目录和媒体/字幕扩展名已在 `.gitignore` 中忽略。不要把这些输出目录、日志、Key、Cookie、个人资料或人工笔记强行 `git add -f`；自定义目录也应置于仓库外或确认忽略规则生效。当前 manifest 保存原文件名但不保存完整原路径；文件名和逐字稿仍可能包含个人资料。

## 后续最小 UI 接入建议

在现有来源面板增加一个“本地文件”入口，复用同一导入服务和状态契约；显示字幕与 ASR 两种来源、独立云端授权及进度，完成后先展示原文/知识笔记并提供文件导出。后续再给 `VideoCache` 或来源索引增加显式本地来源映射，避免把本地 hash 伪装成抖音 ID 或链接。应先解决本地任务恢复和笔记保留，再接 embedding/RAG；本阶段没有实现该 UI。
