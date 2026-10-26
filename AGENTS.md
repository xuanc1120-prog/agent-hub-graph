# Agent Hub 接手入口

本项目处于开发阶段。先读 `development-tasks/handoffs/2026-10-21-course-correction-integration.md`、`development-tasks/handoffs/2026-09-20-course-correction.md` 和 `agent-hub-task-allocation.md`；任务状态以当前 Git 与这些材料核对，旧 handoff 仅作历史证据。

- 用户已授权 2026-09-20 纠偏：优先交付简单图、执行、diff、审批的最小闭环；可先 Mock，再接一个真实 Adapter。
- HUB-300 已通过 PR #7 合入 `main@1f1715d`；不要按旧简报重复开工。下一项主任务优先 HUB-400 最小 API 切片，先接 Mock，不等待 HUB-310 或整个阶段 3。
- 2026-10-21 分工：当前集成任务只整理遗留 TestRunner 修复与纠偏资料；HUB-400 由用户另开的会话实施，使用包含本次修复的已集成基线和独立 worktree。
- 不将所有路线图增强项作为首次 Demo 门槛。普通变更跑相关检查；安全边界变更保留定向负向测试；完整 CI 用于最终集成。不要无原因反复扩大验收。
- 文件隔离、精确恢复、审批证据绑定、凭据隔离、CAS/租约和冻结协议仍有效。缩小交付范围不等于放松这些边界。
- 允许必要修复提交，小型跨模块修复由集成负责人协调，不机械拆任务，不为凑单提交改写历史。
- 交接写明实际 base/head、修改范围、实际验证、未完成项；不能把工作区修改、历史 CI 或其他 Agent 的记录当作自己已复验的结果。
