# 多 Agent / 多 Team 常见失败模式

## 1. 一句话结论

多 team 失败最常见的根因不是 agent 不够聪明，而是完成语义、交付收敛链、通信硬门和 authority root 集成都没有被设计进状态机。

## 2. 范围与资料来源

- 来源：一次真实多 team 运行复盘中暴露出的通用失败现象
- 关注范围：完成语义、任务设计、handoff、bus、reducer、authority root、wave gate
- 关联文档：`README.md`、`delivery-contracts.md`、`runbook-and-checklists.md`

## 3. Runtime 完成被误当成交付完成

### 现象

- 所有 team 都显示 `phase=complete`
- `pending=0`
- `in_progress=0`
- 但 authority root 基本没有推进
- 最终目标并未达成

### 抽象根因

系统只追踪了“任务调度生命周期”，没有追踪“交付生命周期”。

换句话说，系统知道 worker 不再忙，但不知道成果是否已经：

- 被 leader 接收
- 被 reducer 整合
- 被 authority root 合并
- 被端到端验证
- 被上游/下游 lane 消费

### 设计修复

必须显式拆成四层完成语义：

1. `worker_complete`
2. `team_complete`
3. `authority_complete`
4. `objective_complete`

并规定：

- 只有 `authority_complete` 才能被视为“本轮交付完成”
- 只有 `objective_complete` 才能被视为“目标完成”

## 4. 文件所有权驱动的分工，不等于目标驱动的分工

### 现象

- 每个 team 都有清晰 `owned_paths`
- 但每个 team 只优化局部文件
- 没人对“跨文件、跨 lane 的目标推进”负责

### 抽象根因

“谁能改哪些文件”解决的是安全边界，不是里程碑边界。

如果任务定义只有：

- owned paths
- role
- 一个高层自然语言目标

那 team 会倾向于做：

- 在自己文件里找一个看起来合理的改动
- 跑一组局部测试
- 结束任务

这并不能保证整体目标被推进。

### 设计修复

分工必须从“路径所有权”升级为“里程碑所有权”。

每个 lane 除了 `owned_paths`，还必须有：

- `output_artifacts`
- `acceptance_gates`
- `handoff_to`
- `merge_target`
- `done_when`

## 5. Communication Bus 被设计成存在，但没有被设计成必经路径

### 现象

- 有 `outbox`
- 有 `inbox`
- 有 `handoff`
- 有 `decisions`
- 有 `ledger`
- 但运行结束后这些目录几乎为空

### 抽象根因

bus 只是“可用选项”，不是“执行硬约束”。

一旦通信不是必经路径，worker 更倾向于：

- 在自己局部直接结束
- 给 team leader 发本地消息
- 在 task result 里写“完成了”

系统表面上有协作设施，实际执行却仍然是孤岛。

### 设计修复

把 bus 从“可选工具”改成“状态机必经节点”。

例如：

- 跨 lane 依赖必须先写 `handoff_record`
- reducer 只读取 `handoff/` 和 `decisions/`
- authority root 不接受“只在 task result 里声明完成”的产出
- 没有 `ledger` 事件就不能认定发生过真实交付

## 6. 缺少 Authority Reducer / Integrator

### 现象

- worker worktree 有 commit
- team root 不一定推进
- authority root 更没有自动推进
- 最后成果留在孤立分支或 worker worktree 中

### 抽象根因

系统把“实现”拆给了多个 lane，却没有一个“强制收敛结果”的角色。

如果没有 reducer：

- worker 只需完成本地任务
- leader 只需看到 task 变绿
- 没人必须做 authority merge

这会导致：

- 真实代码存在
- 但主系统没有吸收

### 设计修复

必须单独设立 reducer / integrator lane，负责：

- 收集 team root 的提交
- 决定 cherry-pick / merge 顺序
- 处理冲突
- 在 authority root 上运行统一验证
- 生成最终交付 summary

没有 reducer，就不能宣称系统具备真正的多 team 交付能力。

## 7. Team 内部 dispatch 失败却没有阻止“完成”

### 现象

- dispatch 日志中大量出现 `target_resolution_failed:target_not_found`
- 但 team 最终仍进入 `complete`

### 抽象根因

系统把“消息投递失败”当成了局部告警，而不是完成阻断条件。

这意味着：

- leader 可能没有真正收到 worker 回报
- worker 可能没有真正收到派发
- 但 task 状态机依然能继续闭合

### 设计修复

必须把通信健康纳入完成条件：

- 若 leader/worker mailbox 解析失败，不能进入 `complete`
- 若 dispatch error 超阈值，team 自动进入 `degraded`
- 若 leader 与 worker 之间没有可验证 ACK，task 不能结算

## 8. 验证与实现被硬切开，破坏最小闭环

### 现象

- 实现 lane 不能提交最小必要测试
- 验证 lane 又没有同步拿到实现上下文
- 最终形成“临时脚本验证”而不是正式回归测试

### 抽象根因

把 `tests/` 整体独占给 verification lane，过度追求目录边界整洁，反而破坏交付闭环。

### 设计修复

更合理的做法是：

- 实现 lane 允许提交与改动直接相关的最小必要测试
- verification lane 负责跨模块、回归、交叉、端到端检查
- verification lane 不替代实现 lane 的最小闭环

## 9. 大规模并发平铺，没有 Wave Gate

### 现象

- 所有 lane 同时起跑
- 依赖关系只存在脑中或自然语言里
- 中间没有集成门

### 抽象根因

系统缺少 wave 化执行。

### 设计修复

使用分波推进：

1. 基础契约波
2. 结构能力波
3. 生成与评价波
4. 集成与验证波

每一波都必须在 authority root 过 gate 后才能进入下一波。

## 10. Task 完成条件过于自然语言化

### 现象

- task 描述很长
- 路径也很清楚
- 但仍然无法判断“究竟算不算真的完成”

### 抽象根因

描述里缺少强结构字段。

### 设计修复

一个可执行 task 至少要有：

- `goal`
- `owned_paths`
- `allowed_inputs`
- `output_artifacts`
- `verification_commands`
- `handoff_to`
- `merge_target`
- `done_when`
- `blockers_on_failure`

## 11. 通用结论

多 team 失败最常见的根因不是“agent 不够聪明”，而是：

- 完成语义设计错了
- 交付收敛链缺失
- 通信不是硬门
- authority root 不在状态机里

如果这四个问题不先解决，团队越多，只会制造越多“局部看似完成、整体没有推进”的假象。

## 12. 相关文档

- `README.md`
- `delivery-contracts.md`
- `runbook-and-checklists.md`
