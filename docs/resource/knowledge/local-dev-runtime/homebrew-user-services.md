# Homebrew 用户级服务排查与关闭手册

## 1. 一句话结论

在这台 macOS 开发机上，`cliproxyapi` 不是普通前台进程，而是通过 `brew services` 注册的用户级后台服务；它的自启动载体是 `~/Library/LaunchAgents/homebrew.mxcl.cliproxyapi.plist`，要同时“关闭进程 + 关闭开机自启动”，应优先执行 `brew services stop cliproxyapi`。

## 2. 范围与资料来源

本手册基于这次任务中的本机 CLI 证据整理：

- `ps -ef | rg -i 'cliproxyapi|cliproxy|\bcpa\b'`
- `brew services list`
- `rg -n -i 'cliproxy|\bcpa\b' ~/Library/LaunchAgents /Library/LaunchAgents /Library/LaunchDaemons`
- `launchctl list | rg -i 'cliproxyapi|cliproxy|\bcpa\b'`
- `ls -l ~/Library/LaunchAgents/homebrew.mxcl.cliproxyapi.plist`
- `brew services stop cliproxyapi`

## 3. 这次任务里已确认的事实

### 3.1 运行中的进程身份

执行进程扫描时，命中了：

- `/opt/homebrew/opt/cliproxyapi/bin/cliproxyapi`

这说明用户口中的本地 `cpa (cliproxy)`，当前机器上对应的实际进程名是 `cliproxyapi`。

### 3.2 自启动来源

执行 `brew services list` 时，服务状态为：

- `cliproxyapi started`

同时在用户级 LaunchAgents 中命中了：

- `~/Library/LaunchAgents/homebrew.mxcl.cliproxyapi.plist`

该 plist 内的 label 为：

- `homebrew.mxcl.cliproxyapi`

判断：

- 这不是系统级 daemon，而是当前用户会话下的 Homebrew user service
- 因此优先用 `brew services stop` 卸载最稳妥，不需要先手动删 plist

## 4. 标准关闭步骤

当目标是关闭某个 Homebrew 用户级服务并取消登录自启动时，推荐顺序是：

1. 先用 `brew services list` 确认服务名和状态。
2. 执行 `brew services stop <service-name>`。
3. 再检查进程、`brew services` 状态和 `~/Library/LaunchAgents` 中的 plist 是否已经消失。

在这次 `cliproxyapi` 的实际操作中，执行的是：

```bash
brew services stop cliproxyapi
```

## 5. 这次任务的验证结果

执行关闭后，已经确认：

- `brew services list` 中 `cliproxyapi` 状态变为 `none`
- `ps -ef | rg -i 'cliproxyapi|cliproxy|\bcpa\b'` 不再看到目标服务进程
- `~/Library/LaunchAgents/homebrew.mxcl.cliproxyapi.plist` 已不存在
- `launchctl list` 中不再命中 `cliproxyapi`

这四项同时成立时，可以认为“服务已停止，且当前用户登录自启动已禁用”。

## 6. 失败恢复与边界

如果未来再次看到同名服务自动出现，优先按下面顺序判断：

1. 是否有人重新执行了 `brew services start cliproxyapi`
2. 是否有安装脚本或升级脚本重新注册了 Homebrew service
3. 是否存在另一个名字不同、但指向同一二进制的 LaunchAgent 或守护进程

如果 `brew services stop <service-name>` 失败，再退回到 `launchctl bootout` 或手动处理 plist；但对 Homebrew 管理的用户级服务，优先保持 `brew services` 作为唯一控制入口。

## 7. 相关文档

- `resource/knowledge/local-dev-runtime/README.md`
- `resource/knowledge/local-dev-runtime/macos-system-data-triage.md`
