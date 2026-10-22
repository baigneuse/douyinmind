# Phase 1 前端依赖漏洞分类

复核日期：2026-10-23（Asia/Tokyo）。范围仅 `frontend/package-lock.json` 的 npm 依赖；这不是后端 Python、ffmpeg、浏览器或整个产品的安全审计。没有安装、升级或修改任何前端依赖，未执行 `npm audit fix` 或 `--force`。

## 实际命令和结果

在 `frontend` 执行：

```powershell
node --version
npm.cmd --version
npm.cmd audit --json
npm.cmd audit --omit=dev --json
npm.cmd ls --all --json
npm.cmd ls braces chokidar fast-glob micromatch postcss-nested postcss-selector-parser tailwindcss vite
Get-FileHash -Algorithm SHA256 package-lock.json
```

- 环境：Node `v24.13.1`、npm `11.8.0`。
- 全依赖审计：**8 个受影响包报告：6 high、2 moderate、0 critical**，与 Phase 0 一致。`npm audit` 有漏洞时返回非零是正常审计信号，不能记为测试失败或忽略。
- `--omit=dev`：**0 个已报告漏洞**；不等于生产代码已被证明安全。
- `npm ls --all --json` 完成，没有发现 invalid/missing dependency；所列八包均由 `devDependencies` 的 Tailwind/Vite 链引入。
- 锁文件 SHA-256：`07cabe8f50a6d6b19cdfdb12e6e9e9f2cfae883f67b5f26bfbd9f9b4090f31aa`。本轮未修改 `package.json` 或锁文件。
- 原始 JSON 保存在仓库外的本地证据文件 `phase1-npm-audit.json`、`phase1-npm-tree.json`，本页记录可分享的版本、调用链、结论和复现命令。

`8` 是 npm 按受影响包汇总的数量，不是八种独立利用方式。Tailwind 链的七个包大多传递自 `braces` 和 `postcss-selector-parser` 两个根因；Vite 一行还汇总了五条 advisory。

## 八个包的分类和修复候选

| 包及锁定版本 | 级别 / 范围 | 当前依赖链与可达性判断 | 明确的修复路径与风险 |
| --- | --- | --- | --- |
| `braces@3.0.3` | high；开发/构建 | `tailwindcss → chokidar / micromatch → braces`。深度嵌套的 glob 模式可耗尽栈；本项目 glob 来自固定 `tailwind.config.js`，未找到把字幕、逐字稿或网页输入传给 glob 的路径。恶意源码/配置输入仍是构建风险。 | 本次查询最新仍为 `3.0.3`，公告注明无 patched version。不能声称补丁升级可修；可在后续单独迁移至 `tailwindcss@4.3.3` 的新构建链，或等待上游补丁。 |
| `chokidar@3.6.0` | high；开发/构建 | Tailwind 文件监听器；由 `braces` 传递标记，并非本项目新增运行时 API。 | Tailwind 4.3.3 迁移候选移除当前这条旧监听依赖链。不可将 Tailwind 3 的 chokidar 强制跨主版本替换后当作无风险补丁；监听行为/API 需回归。 |
| `fast-glob@3.3.3` | high；开发/构建 | `tailwindcss → fast-glob → micromatch → braces`；扫描仓库配置的源码路径。 | `npm audit` 的 `fixAvailable: true` 不意味着有独立安全的 fast-glob 补丁，本次受影响范围为 `*`。随 Tailwind 4.3.3 的构建链迁移后重新审计。 |
| `micromatch@4.0.8` | high；开发/构建 | Tailwind/fast-glob 调用，底层 `braces@3.0.3`。未见面向用户的 glob 接口。 | 同上；当前链的 braces 没有已发布补丁，单改 micromatch 版本不能据此保证修复。Tailwind 4.3.3 是 npm 提供的主版本候选。 |
| `postcss-nested@6.2.0` | moderate；开发/构建 | `tailwindcss → postcss-nested → postcss-selector-parser@6.1.5`；处理仓库 CSS。没有在线解析用户 CSS 的功能。 | `postcss-nested@7.0.2` 依赖 parser `^7.0.0`，需最终锁到 parser **≥7.1.6**；同时 Tailwind 3 还直接依赖 parser 6，单升 nested 不足。推荐随 Tailwind 4 迁移；强制 override 涉及主版本兼容风险。 |
| `postcss-selector-parser@6.1.5` | moderate；开发/构建 | Tailwind 直接依赖及 nested 间接依赖；超长扁平 selector 可能导致 CPU 消耗。现有源码 CSS 是可信构建输入，未找到用户内容到解析器的请求路径。 | 公告补丁 **7.1.6**。当前 Tailwind 3 的 `^6.1.2` / nested 的 `^6.1.1` 不会自然解析到 7，不能只重装锁文件。跨版本 override 必须专门验证语法与构建；优先正常迁移父依赖。 |
| `tailwindcss@3.4.19` | high；直接开发依赖 | 上述多个传递风险的汇总；不是七个分别暴露的业务接口。使用现有 JS 配置、PostCSS 插件、`@tailwind` 指令生成 CSS。 | npm 推荐 **4.3.3**，`isSemVerMajor=true`。需改插件为 `@tailwindcss/postcss@4.3.3` 或 `@tailwindcss/vite@4.3.3`，调整 CSS 指令、配置加载和可能变化的样式默认值；必须做 UI/浏览器回归，不在本地导入 MVP 顺带升级。 |
| `vite@6.4.3` | high；直接开发依赖 | 不是静态 `dist` 浏览器代码，但现有 `start-local.py` 在用户机器上启动 Vite 开发服务器；`/__open-in-editor` 在安装代码中注册，Windows editor 漏洞存在条件性可达路径。 | 五条公告均列出 **6.4.4** 为 6.x 修复版本；npm registry 已确认存在（同系列另有 6.4.5）。后续优先做限定到 ≥6.4.4 的小步锁文件更新；现有 `^6.0.0` 允许，但仍须测试、构建、启动、代理和 HMR 验证。无需为此直接跳 Vite 7/8。 |

修复版本核对使用只读命令：`npm.cmd view braces version`、`npm.cmd view vite@6 version --json`、`npm.cmd view vite@6.4.4 version engines dependencies --json`、`npm.cmd view postcss-selector-parser@7.1.6 version engines --json`、`npm.cmd view postcss-nested@7.0.2 dependencies engines --json`、`npm.cmd view tailwindcss@4.3.3 version --json`、`npm.cmd view @tailwindcss/postcss@4.3.3 dependencies --json`、`npm.cmd view @tailwindcss/vite@4.3.3 dependencies --json`。这些只是已发布版本和迁移候选核对，**没有实际安装或证明候选升级后的审计为零**。

## Vite 五条 advisory 的当前场景

| 公告 | 当前项目判断 |
| --- | --- |
| [GHSA-698v-4r3x-3vcv](https://github.com/advisories/GHSA-698v-4r3x-3vcv)，Windows `%` 转义 | 当前平台是 Windows，Vite 内置 editor middleware 存在；利用还依赖可控请求/编辑器环境，未进行利用测试。不能因监听 localhost 就断言不可能触发。 |
| [GHSA-r825-w8mm-5pxw](https://github.com/advisories/GHSA-r825-w8mm-5pxw)，编辑器 CLI 参数解释 | 是否成立取决于实际编辑器及参数。现有开发服务器路径存在，未探查或启动用户编辑器来验证。 |
| [GHSA-587h-7wmj-h5pm](https://github.com/advisories/GHSA-587h-7wmj-h5pm)，Windows 文件存在性检查绕过 | 当前 OS 满足 Windows 条件，可能与上述转义问题组合；优先修复，不实际构造命令执行。 |
| [GHSA-9jrq-w75r-8gcw](https://github.com/advisories/GHSA-9jrq-w75r-8gcw)，自定义 middleware 跨源脚本执行 | `vite.config.ts` 仅 React 插件与 `/api` proxy，项目源码没有自定义 `configureServer` / middleware 实现；未发现满足公告特定利用场景的项目代码。内置或第三方插件全覆盖动态验证未做。 |
| [GHSA-rq7h-c2jc-7f22](https://github.com/advisories/GHSA-rq7h-c2jc-7f22)，非 Windows 路径与 `server.fs.allow` | 公告限定非 Windows；当前 Windows 验收环境不满足这个条件，未来 Linux/macOS 运行仍需处理。 |

源码证据：`frontend/package.json`、`frontend/package-lock.json`、`frontend/tailwind.config.js`、`frontend/postcss.config.js`、`frontend/src/index.css`、`frontend/vite.config.ts`、`start-local.py`。安装包内部核对：`node_modules/vite/dist/node/chunks/dep-Dm0c1Wj2.js` 中 `/__open-in-editor` middleware 注册；该文件仅用于本次核对，不提交 node_modules。

## 后续优先级与验证门槛

1. 单独修复 Vite 6.x：选择至少 6.4.4、重建锁文件，执行原前端 72 项测试、构建和本地启动/代理/HMR smoke，再复跑全量与生产 npm audit。不能只看 `npm audit` 数量下降就宣称 UI 兼容。
2. 单独规划 Tailwind 4.3.3 迁移：先验证颜色、字体、表单、弹层、响应式布局及 Markdown 排版；父依赖迁移后再检查旧 `braces` / parser 链是否全部移除。
3. 修复前维持仅 loopback 的开发服务器，不扩展为公网或局域网服务；使用可信源码/配置构建。Phase 1 CLI 本身不启动 Node/Vite，不把这些前端构建库引入媒体处理路径。

底层公告依据：[braces GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)、[PostCSS parser GHSA-rj75-hqrm-r3gf](https://github.com/advisories/GHSA-rj75-hqrm-r3gf)。Tailwind 的主版本迁移要求见[官方升级指南](https://tailwindcss.com/docs/upgrade-guide)。依赖漏洞数据库会更新，本报告只陈述上述日期及锁文件的观测，不代替下一次发布审计。
