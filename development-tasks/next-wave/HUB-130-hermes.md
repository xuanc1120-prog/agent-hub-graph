# HUB-130：Context、Artifact 与 Event 基础设施

任务 ID：`HUB-130`

Owner：Hermes + MiMo-V2.5-Pro

Reviewer：Codex

分支：`agent/hermes-context-artifacts`

Worktree：`E:\agent_hub_worktrees\hermes-context-artifacts`

依赖：已合并的 `HUB-100`、冻结协议标签 `contracts-frozen-v1`

## 目标

实现 `HUB-110`、Console 和真实 CLI Adapter 后续都会依赖的 Context、Artifact 与 Event 基础设施。所有输入输出必须使用冻结的 Pydantic 契约；文件系统内容、SQLite 元数据和事件序列必须可验证、可追踪且 fail closed。

## 允许修改

- `context/context_builder.py`
- `context/task_bundle.py`
- `context/__init__.py`
- `storage/artifact_store.py`
- `storage/artifact_repository.py`
- `storage/event_registry.py`
- `storage/event_repository.py`
- `storage/__init__.py`
- 对应的 `tests/context/**`、`tests/storage/**` 和 `tests/fixtures/artifacts/**`
- 本任务交付说明；只有出现真实架构决策时才新增 ADR

## 禁止修改

- `protocol/**`、`docs/contracts/**` 和冻结 TypeScript 契约
- `migrations/**`；若现有 schema 无法支持实现，停止并报告给 Codex
- `context/planner_bundle.py`
- `master/**`、`workflow/**`、`workspace/**`、`security/**`
- `app/config.py`、FastAPI、CLI、Console、Adapter 和前端
- 任何 Git merge、push、reset 或对其他 worktree 的修改

## 必须实现

### ArtifactStore

1. Artifact 路径只能由服务端根据 `artifact_id/type` 生成，拒绝绝对路径、`..`、symlink/reparse point、hardlink 和根目录逃逸。
2. 先在目标目录写临时文件并计算 SHA-256/大小，再使用同文件系统原子 rename；数据库失败时清理文件，崩溃遗留 temp/orphan 可按显式 TTL 清理。
3. SQLite 只保存冻结 `Artifact` 元数据。任务 owner 与 planner owner 互斥且必须属于同一 session；单 artifact 和 session 总量限制在写入事务内校验。
4. 读取或物化前重新验证 containment、文件类型、hash 和大小。未脱敏 artifact 可以保留为受限证据，但不能进入 TaskContextBundle。
5. 目录和文件使用当前用户专属权限。平台无法证明权限设置成功时必须拒绝写入，不能静默 best effort。
6. 为后续 Console 的“artifact + message 同事务”保留可组合的 staged-write/transaction 接口，不在本任务实现 ConsoleRepository。

### EventRegistry 与 EventRepository

1. 每个 `event_type` 必须注册到唯一 `StrictModel` payload 类型；重复注册、未知类型、错误模型和未知字段均拒绝。
2. Repository 只接受已由 Registry 验证的 typed payload，使用 `canonical_json`，按 UTF-8 bytes 检查 `<= 65_536` 后落库。
3. workflow run event 在同一 `BEGIN IMMEDIATE` 事务中从 `workflow_runs.next_event_seq` 分配连续序号并插入，禁止 `MAX(seq)+1`。
4. 非 run event 的 `workflow_run_id/run_seq` 同时为空；run event 必须同时带 `workflow_id/workflow_run_id/run_seq`。Repository 在事务内验证 session、workflow 和 run 的归属关系，不能只验证 ID 格式。
5. 提供有界读取：按 session 的 event id 或按 run 的 `after_run_seq` 正序读取，默认/最大 limit 明确且不超过 500。
6. 并发追加必须产生唯一、严格递增的 run sequence；失败事务不能消耗或发布半条事件。

### ContextPack 与 TaskContextBundle

1. ContextBuilder 从 `TaskPackage` 和 typed upstream 输入构造冻结 `ContextPack`，不得重新采用 AuthorGraph candidate 权限，也不能扩大 `effective_*` 或 capability scope。
2. ContextPack 只内联有界摘要和元数据；长内容只保留 `ArtifactRef`。预算裁剪必须确定性并返回被省略项记录，不能静默丢失。
3. TaskContextBundle 只物化属于当前 session/task 且 `redacted=true` 的 artifact；每次物化重新校验 metadata、hash、size 和归属。
4. bundle 使用服务端文件名，位于 `DataPaths.agent_runs/<task_id>/context/`，目录私有、文件只读，不暴露 ArtifactStore 或其他 task 的路径。
5. manifest 使用严格模型，记录 source ref、materialized relative path、过期时间和 canonical bundle hash；hash 不信任磁盘中自报值。
6. bundle 总字节上限由调用方显式注入，不在模块内散落 magic number。超限、碰撞或现有不安全目录均拒绝。
7. 提供幂等清理；清理失败返回 typed 结果供后续 security event 使用，本任务不自行修改 workflow 状态。

## 必须测试

- Artifact 路径逃逸、绝对路径、symlink/reparse、hardlink、hash/size 不一致和原子失败清理。
- 单 artifact/session quota、owner 互斥、未脱敏内容禁止物化和跨 session/task 访问拒绝。
- Registry 重复/未知类型、StrictModel extra 字段、UTF-8 payload 超限。
- 两个连接并发 append 时 run sequence 唯一且递增；事务回滚不留下 event 或错误 next sequence。
- Context 权限不扩张、确定性预算裁剪、bundle manifest/hash、只读权限和幂等清理。
- `python -m ruff check .`、`python -m ruff format --check .`、`python -m pytest` 全部通过。

## 不在本任务范围

- StreamingRedactor、ConsoleChunk、WebSocket 和广播。
- GraphExecutor、NodeHandler、Scheduler、Guard、Approval、Git workspace。
- Artifact API 下载、quarantine 产品流程和真实 Agent 调用。
- 对冻结协议或数据库 migration 的顺手修改。

## 交付

只提交一次，建议提交信息：

```text
feat(HUB-130): add context artifact and event infrastructure
```

交付报告必须包含：实际 base commit、修改文件、关键 API、测试结果、平台权限测试情况、残余风险和建议给 `HUB-110` 的集成方式。不要 merge 或 push。
