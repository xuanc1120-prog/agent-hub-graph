# 2026-10-21 遗留修复与纠偏资料集成交接

## 实际基线与范围

- Repository：`E:/agent_hub_graph`。
- Branch：`codex/integrate-course-correction`。
- PR base：`main@1f1715dfec0df7370a09c0a825f62203dc114c2d`，已包含 HUB-300 PR #7。
- 修复提交：`9b4186a`；与 main 的组合提交：`dbdee923a110127f2778740eca23f7d06d47ee9e`。最终文档提交以 `git log`/PR head 为准。
- 交付内容：收拢 2026-09-20 未提交的 TestRunner 修复、回归与纠偏资料，更新 HUB-300 已合并状态和 HUB-400 优先级。
- 未修改 `protocol/**`、`adapters/**` 或业务 API；未实现 HUB-400。
- 原有未跟踪文件 `tmpq8eohys6` 保留，不提交、不删除。测试数据在忽略的 `.tmp/correction-20261021-*` 与 `.tmp/ci-20261021*`。
- 集成分支已推送，2026-10-26 已创建 [待审 PR #8](https://github.com/xuanc1120-prog/agent-hub-graph/pull/8)。此前 GitHub 连接器创建 PR 返回 403、Computer Use 无法确认 Edge URL 的限制未通过浏览器重试解决；本次使用 Git 的既有登录完成 PR 创建。
- 合入 main 前仍须独立复审和当前 Head 的 PR CI；不要将已创建 PR 写成已经合并。

## 行为变化

- TestRunner 在 Windows 先 suspended spawn，加入 Job 后才 resume；正常、失败、超时、取消、输出超限均统一回收进程树。
- stdout/stderr 共享原始字节预算；超限终止、`passed=False`，测试节点记录 `test_output_limit_exceeded`，不能进入审批。
- 结果输出按配置的 UTF-8 字节预算限制，支持默认 10 MiB 范围内超过 100 万字符的日志；超限输出只保留固定提示。
- spawn/cleanup 边界的重复取消不会取消内部清理任务；清理结束后传播 `CancelledError`，同时显式关闭 asyncio transport。
- Ruff 在 Python 3.11 job 执行一次，完整 Python 兼容矩阵与各环境依赖审计保留。

## 2026-10-21 本会话验证

- 修补前定向基线：`80 passed, 1 skipped`。
- 修补后安全、Runner、故障注入与 OpenAPI 定向：`82 passed, 1 skipped`。
- 两个新增的重复取消用例分别覆盖 spawn、cleanup 边界；检查根进程已退出及 transport 已关闭。
- Mock 工作流：`4 passed`，覆盖正常审批、输出超限失败、工作区副作用恢复、Git objects 污染拒绝。
- 全仓 Ruff check/format、compileall、`git diff --check`、`pip check`：通过。
- 前端 lint、Vitest `7 passed`、production build：通过。
- 组合提交 `dbdee92` 的全量 Windows pytest：`899 passed, 27 skipped`，781.79 秒，无失败或 warning。其后仅修改交接文档。本次 PR CI 与独立复审尚待完成，不引用历史绿灯代替。
- 沙箱内工作流首次运行在创建 ArtifactStore 私有目录时出现 `WinError 5`；沙箱外使用新 basetemp/cache_dir 后上述 4 项业务断言全部通过，未修改源码或数据目录 ACL 规则。

## 2026-10-26 PR CI 与依赖更新

- 首轮 CI：run `40286694342`，待审 head `90fabbd`。Python 3.11 全量 `913 passed, 13 skipped, 1 warning`，但 pip-audit 检出 3 个包中的 7 项漏洞；前端 lint、Vitest `7 passed`、build 通过，npm audit 检出高危依赖，Playwright 因前序失败未执行。其他 Python job 受 fail-fast 取消，不能计为通过。
- 为解除实际审计阻塞，仅刷新锁文件：certifi `2026.6.17 → 2026.10.19`、filelock `3.29.7 → 4.2.1`、urllib3 `2.7.0 → 2.8.0`；前端在现有 semver 范围内更新 Vite/Vitest、source-map-js、undici、css-tree 及其依赖。
- Python 锁文件由 uv 定向升级并重新生成 hashes，其余 Python 包版本保持不变；前端仍保留 Vite 8 / Vitest 4 主版本，将 manifest 修复下限提高到 Vite `^8.3.5`、Vitest `^4.1.12`。未修改 UI 源码或审计阈值。filelock 的 major 升级是修复公告指定版本，需在完整 CI 中验证。
- 修复依据包括 [source-map-js 公告](https://github.com/advisories/GHSA-68fv-2mgg-jv7q)、[Vite/launch-editor 公告](https://github.com/advisories/GHSA-698v-4r3x-3vcv) 及 CI 输出的 Python 安全公告。
- 更新后的本地 pip-audit --strict：`No known vulnerabilities found`。前端中间版本 Vite 8.3.5 / Vitest 4.1.11 的 lint、Vitest `7 passed`、production build 通过。
- 此后将 Vitest/mocker 补到 4.1.12，一并关闭 [手动 mock 注册注入](https://github.com/advisories/GHSA-qxg6-89mg-rqv8) 和 [Windows mock 路径检查绕过](https://github.com/advisories/GHSA-xp26-3454-mjgp)；干净 npm ci 后核对实际 Vitest/mocker 均为 4.1.12，lint、Vitest `7 passed`、production build 通过，最终 npm audit 报告 `found 0 vulnerabilities`。
- 完整矩阵与 Playwright 以依赖更新后当前 Head 的 PR CI 为准；尚未获得独立复审批准。

## 2026-10-26 CI 复现的 WAL 初始化竞争

- 依赖更新提交 `ef40b3f` 的 run `40288565790` 中，Python 3.11 在 `test_concurrent_initializers_share_one_migration_lock[fresh]` 失败：迁移提交后的 `PRAGMA journal_mode=WAL` 与另一个初始化者竞争，返回 `SQLITE_BUSY`。Python 3.12 完整 job 通过；3.13 测试与审计步骤成功，但 job 被 fail-fast 标记取消，不能计为完整通过。
- 仅针对这个实际失败点补修：连接配置和迁移后 WAL 设置共用有界重试，仅重试 SQLite 的 `SQLITE_BUSY` 主错误码；其他 SQL 错误直接传播，未成功切到 WAL 时拒绝继续。连接配置失败/取消时关闭未交付的连接；`BEGIN IMMEDIATE`、迁移事务、CAS 与 fencing 校验未改变。
- 新增 5 个回归实例：初始化/普通连接的瞬时 busy 恢复、真实持锁导致的有界失败、非 busy 错误不重试、取消等待后关闭连接。修复前选择的 4 个实例均失败，修复后数据库测试 `17 passed`；既有 fresh/v1 并发初始化测试额外独立运行 10 轮，20 项全部通过。
- 数据库、lease、基础 Repository 与 idempotency 组合回归：`33 passed`；Ruff check/format、compileall、diff-check 通过。
- WAL 设置必须位于事务外，相关行为参考 [SQLite WAL 文档](https://sqlite.org/wal.html)；本次没有重开历史存储全局审计或变更 schema。

## HUB-400 新会话接手

1. 当前主任务优先 HUB-400 最小 API 切片，由用户另开的 Codex 会话实施；本会话只完成本次集成。
2. 优先从包含本 PR 的最新 `main` 创建 `codex/hub-400-api` 和独立 worktree，记录完整 base SHA。若先做准备，集成前须同步本次安全修复，不能把未合并分支写成已发布基线。
3. 先接 Mock：Session、plan、workflow read/update/validate/run、状态查询、diff/artifact、批准/拒绝。复用已有 Service、持久化幂等、CAS、lease/fencing 和 scheduler。
4. 鉴权、请求体限制、同源与 artifact 所有权验证随已开放端点一起交付；状态先轮询，未完成 ticket/Origin 验证前不开放 WebSocket。
5. HUB-310 由 OpenCode 独立推进，HUB-320 由 Hermes 按缺口补测；两者不是 HUB-400 的前置依赖。HUB-410 在实际可用 API 上接最小 GUI。

## 待独立复审及残余范围

- 由独立 Reviewer 核对 Job assignment 失败、父进程先退出、共享输出预算、重复取消和工作流审批阻断；本会话的本地验证不代替独立复审。
- HUB-300 的 `proc.transport.close()` P2 留给 Runner Owner，记录于 HUB-300 简报，本次不跨改 Owner 文件。
- POSIX 主动 `setsid()` 逃逸与真实 Agent 宿主隔离属于 HUB-330；不将 Job/process group 当作 OS sandbox，也未新增 Master 硬崩溃恢复承诺。
