# HUB-400：本地 FastAPI 与调度 API

## 基本信息

- Owner：Codex
- Reviewer：Claude Code / Claude Opus 4.8
- 状态：`in_progress`；最小 Mock API 已实现，正在收尾定向验证与交接
- Base：`3afbb498230e48a9a26d127001f6c585792cb881`（包含 HUB-300 PR #7 与纠偏/TestRunner PR #8）
- Branch：`codex/hub-400-api`（保留名）
- Worktree：`E:\agent_hub_worktrees\codex-hub-400`（独立 worktree；初始 HEAD 等于上述 base）
- 依赖：HUB-110、HUB-210，均已完成

## 启动条件

不再等待整个阶段 3。先读 [修正确认](../handoffs/2026-09-20-course-correction.md)，从包含本轮修正的最新集成基线创建 worktree；先接 Mock，不把真实 Adapter 尚未完成当成 HTTP/UI 的阻塞条件。未经验证的真实执行入口不能启用。

## 目标

在现有 Application Service、Repository、Scheduler、Approval 和 Recovery 之上提供
本地 FastAPI 网络入口与同源静态 GUI 托管。HTTP/WS 只做协议适配，不另写状态机，
不绕过持久化幂等、CAS、lease/fencing 或 durable scheduler。

## 计划 Owner 路径

- `app/api.py`、`app/main.py`、`app/services.py`、必要的 `app/config.py`/`app/cli.py`
- `web/backend/**`
- API 专项测试与本任务资料
- 若 migration/Repository 确有缺口，先写边界说明并保持最小修改

禁止修改 `protocol/**`、`adapters/**`、`web/frontend/**`、Workspace/Guard 安全语义。

## 实现边界

1. `/healthz` 外全部路由挂在统一 bearer-protected router；随机 demo token 使用
   `secrets.compare_digest`，docs/OpenAPI 默认关闭。
2. 仅监听 loopback，严格 TrustedHost/CORS/Origin；token 不进 URL、localStorage、
   bundle 或 Agent 子进程环境。
3. ASGI 层强制实际请求体上限，request/response 使用严格 DTO，不返回绝对路径、
   traceback 或数据库行。
4. 所有 mutation 使用持久化 `Idempotency-Key`；同 key 同请求重放，同 key 请求漂移
   返回 409。语义/layout/approval/fencing 冲突均映射为稳定 409 错误。
5. plan/run 只提交 durable scheduler 并返回 202；API 不直接执行 Agent，也不暴露
   单 task run 旁路。
6. 首次切片只支持轮询；WebSocket/WS ticket 未开放，待 ticket 与 Origin 控制完成后再单独交付。
7. artifact/diff 通过 opaque ID、鉴权后的有界流式响应读取，并重新校验 owner/hash/size。
8. `agent-hub serve` 取得 singleton Master lease，同源只读托管受信
   `web/frontend/dist`；SPA fallback 不吞 API/WS 路径。

## 首次闭环验收面

- Session、plan、workflow read/update/validate/run，以及状态查询。
- 最小图编辑所需数据、diff/artifact 查询、approval approve/reject。
- 使用既有 durable scheduler；运行测试前明确测试代码的信任边界。
- 已开放端点的鉴权、幂等、CAS、请求体上限和同源托管负向检查。
- 状态可先轮询；不使用内存临时状态替代持久化状态。

## 当前实现映射与端点

HTTP 层复用 `WorkflowApplication`、现有 repositories、`DurableScheduler`、ApprovalManager
和启动 Recovery；plan/run 持久化后返回 `202`，没有单 task 旁路。当前最小端点为：

- `GET /healthz`：唯一公开健康检查。
- `POST /api/v1/sessions`：创建 Session（`201`）。
- `POST /api/v1/sessions/{session_id}/plans`（`202`）、
  `GET /api/v1/sessions/{session_id}/planner-runs/{planner_run_id}`：提交 Mock plan 并轮询。
- `GET/PUT /api/v1/sessions/{session_id}/workflows/{workflow_id}`、
  `POST .../validate`：读取、以 semantic-version CAS 编辑并验证 workflow。
- `POST .../runs`（`202`）、
  `GET /api/v1/sessions/{session_id}/workflow-runs/{workflow_run_id}`：提交 durable run 并轮询。
- `GET /api/v1/sessions/{session_id}/artifacts[/{artifact_id}]`：列出 opaque metadata、验证 owner/hash/size 后读取有界内容。
- `GET /api/v1/sessions/{session_id}/change-sets/{change_set_id}` 与
  `GET /api/v1/sessions/{session_id}/change-sets/{change_set_id}/diff`：读取变更摘要与经验证的 canonical patch。
- `GET /api/v1/sessions/{session_id}/approvals/{approval_id}`、
  `POST .../approve`、`POST .../reject`：读取并决定 ChangeSet Approval。

除 `/healthz` 外的 HTTP API 都在 bearer-protected router 下。mutation 需要持久化
`Idempotency-Key`。当前未修改 `protocol/**`、`adapters/**`、`web/frontend/**`、migrations
或 Workspace/Guard 安全语义。

## 必要的 Repository/Scheduler 接口补齐

实现中发现三个具体接口缺口，因此只补充了既有存储/调度类型的内部能力，没有新增 schema 或重写运行时：

1. **原子 mutation**：部分 Repository 只提供自行开启事务的入口，无法与持久化
   Idempotency-Key 和 Master fencing 放入同一事务。新增 `*_in(transaction, ...)` 变体，
   原有方法继续保留并委托到事务内实现。
2. **durable Mock plan**：既有 planner service 是同步调用，scheduler 只消费 WorkflowRun；
   API 不能直接执行 plan。复用现存 `planner_runs` 状态表和事件，增加 pending 创建、
   scheduler handler 路由及 Recovery 对残留 running plan 的 orphan 收敛，不新增内存队列。
3. **Artifact 有界读取**：既有 `ArtifactStore.read_bytes` 没有读取上限，Repository 读取时也
   不能按 session/task lineage 做 API 所需校验。为读取 API 增加实际字节上限及 owner/hash/size
   验证参数；ChangeSet patch 读取同时受单 artifact 与总读取预算限制。

这些变化限于 `storage/**`、`workflow/scheduler.py`、`workflow/recovery.py` 与
`app/services.py` 的适配接口，并由本切片的端到端和定向存储测试覆盖。

## 本地交付状态（2026-10-27）

- 本任务从 base `3afbb498230e48a9a26d127001f6c585792cb881` 的独立分支/worktree 开始；提交 SHA 以本次交付对应的 Git 记录为准。
- 定向 API/service/repository/HUB-200/HUB-210 pytest：`64 passed, 1 skipped`（158.46 秒）；随后针对非 ASCII Bearer 原始字节头的鉴权回归：`1 passed`。
- Ruff check：通过；Ruff format：`162 files already formatted`；`git diff --check`：通过。
- 测试使用新的短路径 basetemp/cache；较长 Windows TEMP 路径曾触发 Git `Filename too long`，换用独立短路径后定向套件全绿。此前的 ACL 拒绝没有通过放宽安全控制规避。
- 尚未运行全量 pytest、Linux/Docker gate、完整 CI 或前端门禁；本任务未修改前端，静态 `dist` 当前不存在。
- 未 push、未创建 PR、未 merge；交付后等待独立安全复审与完整 CI。

以下是后续完整范围，不要求首次切片一并完成。实际开放能力才需要对应控制；ticket/Origin 验证完成前不开放 WebSocket。

## 后续完整验收面

- Session、plan、workflow read/update/layout/validate/run
- workflow run 状态、节点、事件、pause/resume/cancel/rerun
- task/artifact/diff 查询
- approval approve/reject/renew、workspace recovery
- ws-ticket 与 workflow event replay
- 鉴权、幂等、CAS、分页、请求体上限、Origin/ticket 和同源静态托管的负向测试

HUB-410 依赖这些 API 构建 React Flow；HUB-420 接入 ConsoleStream；HUB-430 再做
独立 API/WS/前端故障注入。实际启动前需根据届时生产接口重新核对本任务书。
