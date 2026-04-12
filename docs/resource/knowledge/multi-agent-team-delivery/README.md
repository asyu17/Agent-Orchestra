# multi-agent-team-delivery 知识包

## 1. 一句话结论

多 team 设计的核心不是“能不能起很多队”，而是每个队的输出是否被强制收敛为 authority root 上可验证的结果。

## 2. 这个知识包解决什么问题

这不是一份只对应单次事故的复盘，而是一份可复用的多 agent / 多 team 交付知识包。

它主要解决这些问题：

- 为什么多 team 都显示 `phase=complete`，真实目标却没有推进
- 为什么 worker 已经写出代码，authority root 却几乎没有变化
- 为什么有 artifact bus、liaison worker、team status，协作仍然可能空转
- 下一次该怎样设计，才能让“team 完成”真正等价于“目标推进”

## 3. 范围与资料来源

本知识包抽象自一次真实的多 team 运行复盘，源现象包括：

- team runtime 全部完成
- 多个隔离 root 中存在局部代码提交
- authority root 没有得到对应集成
- coordination bus 基本没有有效流量
- team 内部 dispatch 日志中存在大量 `target_resolution_failed:target_not_found`
- `phase=complete` 与“项目目标完成”发生严重语义错位

这些现象并不只属于某一个代码仓库，而是多 agent 协作系统中的通用失败模式。

## 4. 推荐阅读顺序

1. `failure-patterns.md`
2. `delivery-contracts.md`
3. `runbook-and-checklists.md`

## 5. 文件地图

- `failure-patterns.md`
  - 抽象通用失败模式、症状、根因和设计修复方向
- `delivery-contracts.md`
  - 定义 worker、team、authority、objective 四层完成语义
  - 给出 TaskCard、Handoff、Reducer、Acceptance Gate 的最小契约
- `runbook-and-checklists.md`
  - 给出启动前、运行中、结束判定、失败恢复时的操作清单

## 6. 适用范围

适用于以下类型系统：

- tmux / terminal 多 team orchestration
- 子 agent 并行代码改造
- 多 worktree 并行交付
- 依赖 artifact bus 的跨 team 协作
- 需要 authority root 集成的自动化开发系统

## 7. 维护要求

维护本知识包时，应保持：

- `README.md` 中的阅读顺序与文件地图同步更新
- 新增失败模式、契约或运行手册时回填入口说明
- 完成语义、handoff、authority merge、objective gate 的定义保持一致
