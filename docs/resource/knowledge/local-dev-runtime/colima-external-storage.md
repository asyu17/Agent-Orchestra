# Colima 外置盘迁移手册

## 1. 一句话结论

在当前 macOS + Colima 0.10.1 环境里，把 `~/.colima` 迁到外置硬盘的最稳妥方式是：先停掉 Colima，用能保留稀疏文件的 `rsync -aS` 复制到 `/Volumes/<disk>/.colima`，再把原路径替换成软链接；如果已经在同一个外置盘上，再迁到 `/Volumes/<disk>/Programs/.colima` 这类新路径时，优先直接做同卷 `mv`。

## 2. 范围与资料来源

- 版本与状态证据：
  - `colima version` 返回 `0.10.1`
  - `colima list` 显示 `default` profile，磁盘配置 `60GiB`
- 默认路径与配置证据：
  - `colima template --print` 返回 `~/.colima/_templates/default.yaml`
  - `/opt/homebrew/opt/colima/README.md` 也明确模板落在 `~/.colima/_templates/default.yaml`
  - `strings /opt/homebrew/bin/colima` 中可见 `COLIMA_HOME`
- 当前机器的容量证据：
  - `du -sh ~/.colima` 为 `23G`
  - `df -h ~ /Volumes/disk1` 显示系统盘剩余约 `20Gi`，外置盘 `/Volumes/disk1` 剩余约 `49Gi`
- 迁移后验证证据：
  - `readlink ~/.colima` 指向 `/Volumes/disk1/.colima`
  - `colima status` 正常返回 docker / containerd socket
  - `docker info` 能返回服务端版本与 `DockerRootDir`

## 3. 当前机器上确认过的关键事实

### 3.1 默认目录仍然是 `~/.colima`

当前安装的 Colima 没有把默认宿主目录改成外置盘路径。模板路径、socket 路径和运行日志都继续依赖 `~/.colima`。

判断：

把原始 `~/.colima` 保留为软链接，比只依赖 `COLIMA_HOME` 更稳，因为它不要求每个 shell、GUI launcher 或后台服务都继承额外环境变量。

### 3.2 `.colima` 内含稀疏磁盘镜像

当前机器里至少有两个大文件：

- `~/.colima/_lima/_disks/colima/datadisk`
  - 逻辑大小 `60G`
  - 原目录实际占用约 `21G`
- `~/.colima/_lima/colima/diffdisk`
  - 逻辑大小 `20G`
  - 原目录实际占用约 `1.4G`

这说明 `.colima` 不是普通目录复制问题，而是典型的 sparse image 迁移问题。

### 3.3 `rsync -a` 会把 sparse 文件展开

本机做过最小化实验：

- 先创建一个逻辑 `1.0G`、实际只占 `36K` 的稀疏测试文件
- 用 `rsync -a` 复制到 `/Volumes/disk1` 后，实际占用变成 `1.0G`
- 用 `rsync -aS` 复制后，实际占用只有 `16M`

结论：

在当前机器上，普通 `rsync -a` 会显著放大外置盘占用；`rsync -aS` 能保留 sparse 行为，适合迁移 Colima 镜像。

## 4. 推荐迁移步骤

### 4.1 启动前检查

先确认这些条件：

- `colima list` 显示目标 profile 已停止；如果未停止，先 `colima stop`
- 外置盘挂载路径稳定，例如 `/Volumes/disk1`
- 外置盘剩余空间足够覆盖 `~/.colima` 的实际占用，并保留额外缓冲
- 不要只看 `du -sh ~/.colima`，还要准备在迁移后用 `df -h /Volumes/<disk>` 复核卷级剩余空间
- 理解风险：外置盘未挂载时，`~/.colima` 软链接将失效，Colima 无法启动

### 4.2 正式迁移

首次从系统盘迁到外置盘时，推荐流程：

```bash
TARGET=/Volumes/disk1/.colima
BACKUP="$HOME/.colima.bak-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$TARGET"
rsync -aS "$HOME/.colima/" "$TARGET/"
mv "$HOME/.colima" "$BACKUP"
ln -s "$TARGET" "$HOME/.colima"
```

说明：

- `rsync -aS` 的 `-S` 必须保留
- 先复制、后改名、最后建软链接，是为了在复制失败时保住原始目录
- `BACKUP` 不要立刻删，至少在一次完整启动验证后再决定是否清理

如果已经在同一块外置盘上，例如从 `/Volumes/disk1/.colima` 再迁到 `/Volumes/disk1/Programs/.colima`，更合适的流程是：

```bash
SRC=/Volumes/disk1/.colima
DST=/Volumes/disk1/Programs/.colima

mv "$SRC" "$DST"
rm ~/.colima
ln -s "$DST" ~/.colima
```

说明：

- 同卷 `mv` 不会重新复制虚拟磁盘镜像，风险和耗时都远低于再跑一轮 `rsync`
- 前提是 `colima` 必须处于停止状态
- 目标路径不能预先存在

### 4.3 迁移后验证

最少做下面几步：

```bash
readlink ~/.colima
colima start
colima status
docker context ls
docker info --format '{{.ServerVersion}} | {{.DockerRootDir}}'
df -h /Volumes/disk1
```

如果迁移前机器是停止态，验证通过后再恢复：

```bash
docker context use default
colima stop
```

## 5. 当前任务里的实测结果

本次在当前机器上的第一轮实测结果如下：

- `~/.colima` 已切换为指向 `/Volumes/disk1/.colima` 的软链接
- 保留了一份原目录备份：`~/.colima.bak-20260403-121801`
- 新目录总占用约 `26G`
- 新目录里的大文件实际占用约为：
  - `datadisk` 约 `24G`
  - `diffdisk` 约 `2.1G`
- 迁移完成后再次执行 `df -h /Volumes/disk1`，卷级剩余空间约 `4.7Gi`
- `colima start` 能成功启动，日志里 `hostagent socket` 已落在 `/Volumes/disk1/.colima/...`
- `colima status` 返回正常
- `docker info` 返回 `29.2.1 | /var/lib/docker`
- 为保持迁移前状态，验证后已执行：
  - `docker context use default`
  - `colima stop`

本次在当前机器上的第二轮实测结果如下：

- 已把目录从 `/Volumes/disk1/.colima` 原地移动到 `/Volumes/disk1/Programs/.colima`
- `~/.colima` 现已指向 `/Volumes/disk1/Programs/.colima`
- 旧路径 `/Volumes/disk1/.colima` 已不存在
- `colima start` 日志中的关键运行路径都已切换到：
  - `/Volumes/disk1/Programs/.colima/_lima/colima/ha.sock`
  - `/Volumes/disk1/Programs/.colima/_lima/_disks/colima/datadisk`
- `colima status` 正常
- `docker info` 正常
- `docker run --rm --pull never alpine:3.20 true` 成功，说明本地缓存镜像的容器创建与执行链路正常
- `docker run --rm hello-world` 失败，但根因是访问 Docker Hub 时返回 `EOF`，属于外网/registry 请求问题，不是 `.colima` 路径迁移问题
- 为恢复迁移前状态，验证后再次执行：
  - `docker context use default`
  - `colima stop`

## 6. 已知现象与判断

### 6.1 启动日志中的 `cd: /Volumes/disk1/... No such file or directory`

本次验证启动时出现过几条：

- `bash: line 1: cd: /Volumes/disk1/Document/code/Agent-Orchestra: No such file or directory`

事实：

- 它没有阻止 Colima 进入 `READY`
- `colima status`、`docker context ls` 和 `docker info` 都能正常返回

判断：

这更像是启动过程里某个命令继承了当前宿主工作目录，而该路径不是 guest 内自动存在路径；它不影响 `.colima` 已经迁移到外置盘这一事实。

### 6.2 不要只拿 `du` 当验收结论

本次迁移里：

- `du -sh /Volumes/disk1/.colima` 约为 `24G`
- 但 `df -h /Volumes/disk1` 显示卷级剩余空间只有 `4.7Gi`

事实：

- `.colima` 目录内没有额外的大型隐藏临时文件
- 没有发现 `/Volumes/disk1` 上“已删除但仍被占用”的文件句柄

判断：

迁移这类 Colima 稀疏镜像时，`du` 只能说明目录层面的可见占用，不能替代卷级可用空间判断。真正的验收 gate 应该同时包含 `du` 和 `df`。

## 7. 回滚路径

如果后续需要回滚：

```bash
rm ~/.colima
mv ~/.colima.bak-<timestamp> ~/.colima
```

如果要重新走外置盘迁移，再按本手册第 4 节重做。

## 8. 相关文档

- `README.md`
- `../README.md`
