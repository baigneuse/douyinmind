# Phase 1：收藏同步已知缺陷与回归门禁

复核日期：2026-10-23（Asia/Tokyo）。基准：`4f0bfbf5129cba45e8575562606df9aa04f7ed0b`。

本页保留 Phase 0 本地 `risk_reproductions.py` / `risk_reproductions.json` 的脱敏观察，并提供可提交、可重复执行的回归测试。编号是本仓库文档内的追踪编号，不表示已经建立远程 GitHub Issue。本阶段没有修复这两个收藏同步缺陷，也没有改动原有测试断言。

## 复现和统计口径

```powershell
cd backend
uv run python -m pytest tests/test_known_favorites_defects.py -rx --basetemp ../../phase1-evidence/pytest-known-defects-new
```

测试文件：[`backend/tests/test_known_favorites_defects.py`](../backend/tests/test_known_favorites_defects.py)。复用已有 `tests.test_knowledge_bases.context` 的隔离 SQLite fixture；仅构造合成收藏快照，Chroma 使用真实临时内存库和显式合成向量，关闭遥测，不调用 embedding、模型或真实抖音采集。

两个测试断言的是修复后的期望行为。仅精确复现既知症状时抛出 `KnownFavoritesDefect` 并成为 `XFAIL`；其他异常和断言失败正常失败。`strict=True` 保证缺陷消失但标记未更新时产生 `XPASS(strict)`，使 CI 失败，要求维护者明确去掉标记。**XFAIL 表示缺陷仍在，不能算作功能通过，也不是原套件已有的真实音频实验 SKIP。**

2026-10-23 首次实际执行结果：**2 XFAIL、0 PASS、0 FAIL、0 SKIP**，耗时 10.93 秒。Python 3.12.14、pytest 9.1.1；执行器使用已安装环境的 `.venv/Scripts/python.exe -m pytest`，首次临时目录为 `../.phase1-known-defect-tmp`。该目录清理被自动审批拒绝后原样保留，已加入忽略规则；上面的复现命令改用仓库外的新目录。请先创建 `phase1-evidence` 父目录，每次使用未存在的 basetemp，避免 pytest 清空旧证据目录。

另行关闭预期失败标记验证症状：

```powershell
uv run python -m pytest tests/test_known_favorites_defects.py --runxfail -q --tb=short --basetemp ../../phase1-evidence/pytest-known-defects-unmasked
```

结果为 **2 FAIL**（3.86 秒，退出码 1），分别对应 `FAV-001: duplicate platform_item_id rejected` 和 `FAV-002: SQLite deleted pending, Chroma retained pending:0`。这次刻意的缺陷复现不是原有测试回归；证明 XFAIL 背后仍然是真实可执行的失败断言。日志保存在仓库外 `phase1-evidence/known-defects-unmasked.log`。

## FAV-001：一个视频不能同时属于两个收藏夹

- 输入：两个合成收藏夹、一个视频、两个收藏夹 membership。
- 调用：`FavoritesService._sync_collections` → `_sync_videos_and_cache` → SQLite flush/commit。
- Phase 0 及本次症状：`IntegrityError`，`UNIQUE constraint failed: favorite_videos.platform_item_id`。
- 原因：`FavoriteVideo.platform_item_id` 是全局唯一字段，但同步代码按 `(collection_id, platform_item_id)` 创建多条关联。
- 修复门槛：允许两个关联行和一个 `VideoCache`，重复同步幂等；必须评估数据库迁移及现有重复数据处理。不能只删除 Python 模型上的 `unique=True` 而忽略已有 SQLite 索引。

## FAV-002：取消收藏留下 Chroma 孤儿块

- 输入：两个已处理合成视频，各有一个 Chroma 块。其中 `ready` 在命名知识库中；`pending` 未归类。
- 操作：同步一个空收藏夹快照。
- SQLite 观察：全部收藏关联删除；`pending` 缓存删除；命名知识库中的 `ready` 及完整原文保留。
- Chroma 观察：原两个块都保留；孤儿块为 `pending:0`。
- 原因：`_sync_videos_and_cache` 删除 SQLite 孤儿缓存时未调用 Chroma 清理。
- 修复门槛：仅删除真正孤儿的索引块，保留命名知识库的块；评估 SQLite 提交失败、索引删除失败时的补偿和重试，避免把两个存储的非原子操作误称为事务。

## Phase 1 边界

本地导入不创建 `FavoriteVideo`，也不调用收藏快照同步；首版不要求 embedding。因此这两个缺陷没有位于本地导入的直接执行路径。CLI 必须保持这个边界，后续 UI 集成不得把本地来源伪装成抖音收藏关联。

**重新启用真实抖音批量同步前，必须修复 FAV-001 和 FAV-002 并将对应测试改为正常通过。** 在此之前，两个 XFAIL 是明确的后续阻断项，不是发布背书。
