# HUB-400：本地 FastAPI 与调度 API

## 基本信息

- Owner：Codex / GPT-5.6 xhigh
- Reviewer：Claude Code / Claude Opus 4.8
- 状态：queued，当前不启动
- Base：看板切换时的最新 `main`
- Branch：`codex/hub-400-api`（保留名）
- Worktree：`E:\agent_hub_worktrees\codex-hub-400`（当前不创建）
- 依赖：HUB-110、HUB-210，均已完成

## 启动条件

虽然依赖已满足，当前任务队列仍先完成
`HUB-300 -> HUB-320 -> HUB-310 -> HUB-330`。只有任务看板明确把 HUB-400 切换为
ready 时，才从届时最新 `main` 创建独立 worktree；不得从本次资料分支或历史 worktree
提前开发。

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
6. WebSocket 使用一次性、短期、绑定 subject/origin/target 的 ticket；workflow event
   按 `after_run_seq` 续传。Console 续传接口为 HUB-420 预留，不在 HUB-400 伪造数据。
7. artifact/diff 通过 opaque ID、鉴权后的有界流式响应读取，并重新校验 owner/hash/size。
8. `agent-hub serve` 取得 singleton Master lease，同源只读托管受信
   `web/frontend/dist`；SPA fallback 不吞 API/WS 路径。

## 最小验收面

- Session、plan、workflow read/update/layout/validate/run
- workflow run 状态、节点、事件、pause/resume/cancel/rerun
- task/artifact/diff 查询
- approval approve/reject/renew、workspace recovery
- ws-ticket 与 workflow event replay
- 鉴权、幂等、CAS、分页、请求体上限、Origin/ticket 和同源静态托管的负向测试

HUB-410 依赖这些 API 构建 React Flow；HUB-420 接入 ConsoleStream；HUB-430 再做
独立 API/WS/前端故障注入。实际启动前需根据届时生产接口重新核对本任务书。
