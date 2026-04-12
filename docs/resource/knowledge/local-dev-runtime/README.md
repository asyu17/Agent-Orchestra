# local-dev-runtime 知识包

## 1. 一句话结论

这个知识包沉淀本仓库在 macOS 本地开发时依赖的宿主运行环境知识，当前重点覆盖 Docker / Colima 的宿主目录迁移与外置盘放置。

## 2. 这个知识包解决什么问题

这个知识包主要回答下面几类问题：

- 本仓库本地开发依赖 Docker / Colima 时，宿主侧数据目录应该如何迁移
- 当系统盘空间吃紧时，如何把 `~/.colima` 安全迁到外置硬盘
- 做这类迁移时，哪些文件是稀疏磁盘镜像，哪些复制参数会把外置盘写爆
- 迁移后应该如何验证 Colima、Docker context 和回滚路径
- 当本机某个后台工具通过 Homebrew user service 自启动时，应该如何识别、关闭并禁用登录自启动

## 3. 范围与资料来源

- 本机 CLI 证据：
  - `colima version`
  - `colima list`
  - `colima start --help`
  - `colima template --print`
  - `colima status`
  - `docker context ls`
  - `docker info --format '{{.ServerVersion}} | {{.DockerRootDir}}'`
  - `du -sh ~/.colima /Volumes/disk1/.colima`
  - `df -h ~ /Volumes/disk1`
- 本机安装资料：
  - `/opt/homebrew/opt/colima/README.md`
  - `strings /opt/homebrew/bin/colima | rg 'COLIMA_HOME|XDG_CONFIG_HOME'`

## 4. 推荐阅读顺序

如果任务是处理本地 Docker / Colima 空间问题，建议按下面顺序阅读：

1. `colima-external-storage.md`
2. `macos-system-data-triage.md`

如果任务是处理本机通过 Homebrew 注册的后台自启动服务，建议按下面顺序阅读：

1. `homebrew-user-services.md`

## 5. 文件地图

- `colima-external-storage.md`
  - Colima 默认目录、外置盘迁移步骤、稀疏镜像复制风险、验证与回滚
- `macos-system-data-triage.md`
  - macOS “系统数据”排查路径、可见大户、权限盲区与下一步检查方式
- `homebrew-user-services.md`
  - Homebrew 用户级后台服务的识别、关闭、自启动禁用与验证方式，包含 `cliproxyapi` 实例

## 6. 适用范围

适用于以下任务：

- 本仓库开发机上的 Docker / Colima 宿主环境维护
- 处理 `~/.colima` 占用过大、需要迁到外置硬盘
- 排查 macOS Storage 里 “System Data / 系统数据” 异常偏大
- 管理本机通过 Homebrew `brew services` 注册的用户级后台服务
- 需要为后续 agent 提供可复用的本地运行手册

## 7. 维护要求

维护本知识包时，应保持：

- 包内 `README.md` 的阅读顺序和文件地图同步更新
- `colima-external-storage.md` 中的命令、风险和验证步骤与本机 Colima 版本行为保持一致
- `macos-system-data-triage.md` 中的排查命令、权限限制和常见大户列表与当前 macOS 行为保持一致
- `homebrew-user-services.md` 中记录的服务控制方式与当前 Homebrew `brew services` 行为保持一致
- 如果新增其他本地开发运行时主题，也回填到本文件的文件地图和阅读顺序
