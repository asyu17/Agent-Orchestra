# 多 Team 运行手册与检查清单

## 1. 一句话结论

多 team 系统是否真正结束，必须落在 authority root 的集成结果和 objective gate 上，而不能只看 runtime phase。

## 2. 范围与资料来源

- 来源：多 team 运行复盘中抽象出的启动、监控、结束和恢复经验
- 关注范围：启动前检查、运行中健康信号、结束判定、wave 模型、失败恢复
- 关联文档：`README.md`、`failure-patterns.md`、`delivery-contracts.md`

## 3. 启动前检查

### 3.1 目标检查

在起队之前，先确认本轮目标是不是单轮可完成。

如果不是，必须先拆 wave。

不要把下面这些一起塞进同一轮：

- 基础契约改造
- 核心结构能力改造
- 端到端集成
- 全量验证
- 文档审计

### 3.2 Authority Root 检查

必须明确：

- authority root 是哪个目录
- 哪个分支是唯一交付分支
- 谁有权把 team 结果合回 authority root

如果 authority root 不明确，多 team 结果几乎一定会散落。

### 3.3 Team Card 检查

每个 lane 启动前都必须有：

- `owned_paths`
- `output_artifacts`
- `verification_commands`
- `handoff_to`
- `merge_target`
- `done_when`

任何缺一项，都说明 task 还不够结构化。

### 3.4 Coordination Bus 检查

必须检查：

- `registry` 已准备
- router 处于可运行状态
- `outbox/inbox/handoff/decisions/ledger` 目录存在
- liaison worker 的写权限边界清楚

更重要的是：

- 没有 bus gate 的 team，不要启动

## 4. 运行中监控

### 4.1 不要只看 team phase

`phase=complete` 只能说明 runtime 空闲下来，不能说明目标完成。

必须同时看：

- authority root 是否新增有效 commit
- `handoff/` 是否有真实交付
- `ledger` 是否有真实流量
- reducer 是否已有集成记录
- objective verification 是否推进

### 4.2 必看的健康信号

### 信号 A：dispatch error

若持续出现：

- `target_resolution_failed`
- mailbox 不可达
- leader 不回 ACK

应立即降级处理，不要继续假设协作正常。

### 信号 B：bus 零流量

若多个 lane 同时运行，但：

- `outbox` 为空
- `handoff` 为空
- `ledger` 为 0

那说明系统没有真正协作，只是在并行单机工作。

### 信号 C：team complete 但 authority root 无变化

这代表 reducer / integrator 链条失效。

### 信号 D：worker result 有 commit，但 leader root 不包含该 commit

这代表 worker 完成和 team 收敛脱节。

## 5. 结束判定

一轮多 team 只有在下面条件同时满足时，才算“真正结束”：

1. 所有必须 lane `phase=complete`
2. 所有必须 lane `failed=0`
3. reducer 已完成 authority merge
4. authority root 统一验证通过
5. objective summary 已生成
6. 没有未消费 handoff
7. ledger 中存在完整交付轨迹

如果只是第 1 和第 2 条成立，那只能叫：

`runtime finished`

不能叫：

`delivery finished`

## 6. 推荐 Wave 模型

### 6.1 Wave 1：基础能力波

- contracts
- artifact store
- parser / signatures
- metadata / history normalization

结束条件：

- authority root 通过基础测试

### 6.2 Wave 2：中层能力波

- relation graph
- template pilot
- local screen
- slot scheduler

结束条件：

- authority root 通过 flow 级测试

### 6.3 Wave 3：集成与验证波

- end-to-end verification
- architecture audit
- integration cuts

结束条件：

- authority root smoke run 成功
- final summary 达标

## 7. 失败恢复 Runbook

当出现“team 全绿但目标没完成”时，按下面顺序恢复：

1. 列出 authority root 相对基线的真实 diff
2. 列出各 team root 相对基线的真实 diff
3. 找出哪些 team 只是局部完成、哪些 team 有可用提交
4. 检查 bus 是否发生真实交付
5. 检查 reducer 是否存在或是否失效
6. 先收割已有有效提交，不要立即重开新一轮
7. 用这次失败补齐 TaskCard / reducer / acceptance gates
8. 再启动下一轮

## 8. 下一轮设计必须避免的事情

- 不要只按文件夹拆 team
- 不要把所有 lane 一次性平铺到底
- 不要把 verification 完全外包给独立测试队
- 不要把 liaison 设计成“可用但可绕过”
- 不要允许没有 authority merge 的 `complete`

## 9. 最小成功定义

对任何多 team 系统，最小成功定义都应该是：

`team 结果被 reducer 吸收，并在 authority root 上通过统一验证`

如果做不到这件事，系统仍然只是“多队并行编辑器”，还不是“多 team 交付系统”。

## 10. 相关文档

- `README.md`
- `failure-patterns.md`
- `delivery-contracts.md`
