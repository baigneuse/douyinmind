# Phase 1 公开媒体离线导入验收

验收日期：2026-10-23（Asia/Tokyo）。本页只记录真实公开媒体与既有字幕的实际 CLI 输入结果；不提交媒体、字幕原文件、逐字稿、知识笔记或人工笔记内容。完整功能测试、已知缺陷和阻断项见 [Phase 1 报告](PHASE1_REPORT.md)，用法见 [本地导入说明](LOCAL_MEDIA_IMPORT.md)。

## 输入来源与处理许可

媒体是 Blender Foundation 制作的 **Sintel 120 秒节选**，包含真实录制的角色对白与音效。用户授权使用公开素材进行验收。文件来自 [Chris Mills HTML5 字幕教学示例](https://github.com/chrisdavidmills/html5-captions-and-subtitles-content-kit/tree/400f234c4dcafb7505f67ee7262f748662d667bf)的固定提交 `400f234c4dcafb7505f67ee7262f748662d667bf`，其 [原始 HTML](https://github.com/chrisdavidmills/html5-captions-and-subtitles-content-kit/blob/400f234c4dcafb7505f67ee7262f748662d667bf/demo/final/index.html)明确将 `demo/video/sintel-short.mp4` 与 `demo/final/vtt/sintel-captions-en.vtt` 配对。

同一视频的 Git blob SHA 为 `e24dd8fcca6df535682ed96338e861bfb41c6a38`，与 `mdn/shared-assets` 中的视频一致。[MDN 官方文章](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Audio_and_video_delivery/Adding_captions_and_subtitles_to_HTML5_video)也说明采用 Sintel 语音节选演示字幕。

- 媒体许可：Blender [官方 sharing 页面](https://durian.blender.org/sharing/)明确为 [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)。署名：**© copyright Blender Foundation | durian.blender.org**。
- 示例仓库自己的 [LICENSE](https://github.com/chrisdavidmills/html5-captions-and-subtitles-content-kit/blob/400f234c4dcafb7505f67ee7262f748662d667bf/LICENSE) 为 MPL 2.0；此记录与影片本身许可分开，不以示例代码许可替代影片授权。
- 未使用账号、Cookie 或任何 API Key。公开文件只下载到本地忽略目录，没有媒体上传或云端识别。
- 官方许可 HTML、示例仓库 LICENSE、原配对 HTML 和完整 `public-media-provenance.json` 保存在仓库外 `phase1-evidence/`。

下载链接：[MP4 原始文件](https://raw.githubusercontent.com/chrisdavidmills/html5-captions-and-subtitles-content-kit/400f234c4dcafb7505f67ee7262f748662d667bf/demo/video/sintel-short.mp4)、[WebVTT captions 原始文件](https://raw.githubusercontent.com/chrisdavidmills/html5-captions-and-subtitles-content-kit/400f234c4dcafb7505f67ee7262f748662d667bf/demo/final/vtt/sintel-captions-en.vtt)。输入字节未经修改，只为验收中文/日文路径改变了本地文件名。

## 可复核输入指标

| 指标 | 观察值 |
| --- | --- |
| MP4 大小 | 22,306,323 字节 |
| MP4 SHA-256 | `358447f332663e6f3794816e32b8072526fcba8bf8744dc7fb3387afb5b2fceb` |
| 媒体时长 | 120.00 秒；manifest 为 `duration_ms: 120000` |
| 媒体流 | H.264 720×306、24 fps；AAC 48 kHz、stereo |
| VTT 大小 | 2,460 字节 |
| VTT SHA-256 | `34441b619a7ae7285b32b35234485abf5ad933c0defc933d3966cc60585c2992` |
| 已发布 captions 数量 | 22；其中 13 个对白，9 个音效/环境声描述 |
| 时间范围 | 第一段起点 0 ms；最后一段终点 119,500 ms |

输入路径（相对仓库根目录）为 `backend/app/storage/phase1_acceptance/公开媒体_日本語/Sintel_短片_日本語.mp4` 和同目录的 `Sintel_原字幕_日本語.vtt`。两者均经 `git check-ignore` 验证被忽略。

该 captions 文件是外部既有发布内容，包含 `<c.*>`、`<b>`、`<i>` 样式标签，以及 `line:` / `align:` 布局设置。解析器保留可见文本与真实 cue 时间，移除展示格式。**22 段不是本工具识别出的 22 段语音**，环境声描述也不是逐字对白。没有使用自行生成的测试字幕，也没有手工调整时间。

曾检查的 MDN `sintel-en.vtt` 有一个作者添加的 `[Test]` cue，未选择它作为本次验收输入；实际使用上面独立发布的 captions track，避免静默删改来源。

## 实际运行

在 `backend`，使用已安装的 Python 3.12 环境，并确保已准备的 ffmpeg 在本次进程 `PATH` 中：

```powershell
.\.venv\Scripts\python.exe -m scripts.import_local --input 'app/storage/phase1_acceptance/公开媒体_日本語/Sintel_短片_日本語.mp4' --subtitles 'app/storage/phase1_acceptance/公开媒体_日本語/Sintel_原字幕_日本語.vtt'
```

日志 `phase1-evidence/real-media-import.log` 记录：校验/SHA-256 → 受控副本处理 → 离线读取字幕 → 原文和来源信息保存，并明确“未生成 AI 总结”。最终 CLI JSON：

```json
{
  "status": "transcript_complete_summary_not_generated",
  "source_sha256": "358447f332663e6f3794816e32b8072526fcba8bf8744dc7fb3387afb5b2fceb",
  "origin": "subtitles",
  "segments": 22,
  "transcript_reused": false,
  "summary_status": "not_requested",
  "source_unchanged": true
}
```

结果位于现有存储区 `backend/app/storage/local_imports/local-media-358447f332663e6f3794816e32b8072526fcba8bf8744dc7fb3387afb5b2fceb/`。已实际生成五份要求的文件：`source_manifest.json`、`transcript.json`、`transcript.md`、`knowledge.md`、`notes.md`，以及规范化音频缓存。没有建立第二套数据库，也没有向 Chroma 写入。

首轮 manifest 验收要点：

| 字段/阶段 | 实际值 |
| --- | --- |
| `source_type` | `local`，没有抖音 URL |
| `pipeline_version` / `normalization_version` | `local-media-v1` / `pcm16-mono-16k-v1` |
| `models.transcription` | provider `subtitles`，模型名 `null` |
| `models.summary` | provider `none`，模型名 `null` |
| `validation` / `normalization` / `transcription` / `export` | 均为 `completed` |
| `summary` | `not_requested` |
| `consent.cloud_asr` / `consent.cloud_summary` | 均为 `false` |
| `source_integrity.sha256_after` | 与输入 SHA-256 完全一致 |
| `source_integrity.unchanged` | `true` |

manifest 的 UTC 时间为 2026-10-22T15:34 左右，对应 Asia/Tokyo 的 **2026-10-23 00:34**，不应因 UTC 日期误认为前一天执行。

## 重复导入、人工笔记与云端阻断

在已生成的 `notes.md` 中添加一段仅用于本地验收的人工备注，记录该文件散列，再用完全相同的离线命令重跑。证据为仓库外的 `real-media-repeat.log` 与 `real-media-acceptance.json`，没有把备注正文复制进本报告。

| 重复验收项 | 实际结果 |
| --- | --- |
| 退出码 | `0` |
| 规范音频 | 日志明确“复用已校验的规范音频” |
| 逐字稿 | `transcript_reused: true`，日志明确“没有新转写请求” |
| 逐字稿 SHA-256 | `503c6a1ba8eafd2dd21120af62ade6e8f80509c762b65bd81597fc994b729d77`，复用后保持 |
| 人工 `notes.md` | `notes_preserved: true`，加入验收备注后的前后 SHA-256 一致 |
| 输入媒体/字幕 | MP4 与 VTT 仍为上表 SHA-256，原文件未改变 |
| 必须的五个输出文件 | `required_files` 五项均为 `true` |
| 来源/时长/片段 | 仍为 `subtitles`、120,000 ms、22 个片段，边界 0–119,500 ms |
| AI 总结 | 仍为 `not_requested` / `NOT_GENERATED` |

另在独立输出目录执行同一真实 MP4 的 `--mode qwen`，不提供 `--allow-cloud-asr` 或有效凭证。`real-media-asr-blocked.log` 记录 **退出码 3**，错误码 **`cloud_asr_not_authorized`**。程序在云端授权门禁停止，未创建并调用 Qwen 客户端，服务请求为 0。这是“默认禁止上传”验收，**不是 Qwen 真实转写通过**。

## 通过范围与未通过范围

- **已通过：** 真实公开本地 MP4 输入；中文/日文路径；真实 ffmpeg 解码和受控副本规范化；已发布 WebVTT captions 导入；22 个结构化时间片段；五文件导出；重复导入复用；人工笔记不覆盖；原始 MP4/VTT SHA-256 不变；未授权 ASR 返回阻断。
- **已有字幕，不是 ASR：** 字幕文本来自固定来源文件。本次没有人工逐词复核媒体与全部字幕，不能证明字幕覆盖全部语音或识别准确率。
- **未生成 AI 总结：** `knowledge.md` 是如实标记状态的结构化文档；摘要、主题、关键论点、重要数字不伪造为已完成模型结果。
- **真实 Qwen ASR / 真实 DeepSeek 笔记：BLOCKED。** 本阶段没有用户提供的有效凭证及真实云端调用授权；本页不把 Mock 测试计为真实外部服务验收。
- **总结失败恢复、无效媒体、空转写及其他格式：** 本页的真实 MP4/VTT 样例不覆盖全部情况；对应自动化测试和实际验证类别由 Phase 1 主报告分别列明，不扩大本样例的通过范围。

本页不保存原文/人工笔记正文，也不把公开素材嵌入源码仓库。验收用数据、逐字稿和输出继续留在忽略的本地存储目录中。
