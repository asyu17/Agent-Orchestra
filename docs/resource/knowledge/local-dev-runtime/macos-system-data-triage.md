# macOS 系统数据排查手册

## 1. 一句话结论

当前这台 macOS 机器上，`Storage` 里的“系统数据”偏大并不是由本地 Time Machine 快照造成；可见的大头集中在 `~/Library`、`/private/var`、Homebrew、Apple 机器学习/字体/翻译资产，以及 APFS 的 `Preboot` / `VM` 相关卷，但仍有一块 Data 卷根部的 Apple 受保护隐藏目录因为终端没有完全磁盘访问而无法做精确文件级归因。

## 2. 范围与资料来源

- 机器级容量事实：
  - `df -h / /System/Volumes/Data ~ /Volumes/disk1`
  - `system_profiler SPStorageDataType`
  - `diskutil apfs list`
  - `diskutil info /System/Volumes/Preboot`
- 可见目录扫描：
  - `du -sh /Users/*`
  - `du -sh ~/Library /Library /private/var/vm /private/var/folders`
  - `du -sh /private/var/*`
  - `du -sh /Library/*`
  - `du -sh ~/.colima ~/.docker ~/.codex ~/.Trash`
  - `du -sh ~/Library/Parallels ~/Library/Developer ~/Library/Caches/JetBrains ~/Library/Caches/ms-playwright ~/Library/Application Support/Code ~/Downloads ~/Desktop`
  - `find ~/Library/Application Support -mindepth 1 -maxdepth 1 -type d -exec du -sh {} +`
  - `find ~/Library/Caches -mindepth 1 -maxdepth 1 -type d -exec du -sh {} +`
  - `find ~/Library/Group Containers -mindepth 1 -maxdepth 1 -type d -exec du -sh {} +`
  - `find ~/ -maxdepth 6 -type f -size +1G -exec ls -lh {} +`
  - `osascript` 调 Finder 的 `size` 属性递归统计 `~/Library`、`~/Library/Containers`、`~/Library/Caches`、`~/Library/Application Support`、`~/Library/Parallels`
  - `du -sh /System/Volumes/Data/System/Library/AssetsV2 /System/Volumes/VM /System/Volumes/Data/usr/local /System/Volumes/Data/opt/homebrew`
  - `find /private/var/db/diagnostics -mindepth 1 -maxdepth 2 -exec du -sh {} +`
- 快照与系统层验证：
  - `tmutil listlocalsnapshots /`
  - `diskutil apfs listSnapshots /System/Volumes/Data`
  - `lsof +L1`
  - `mdutil -h`
  - `man mdutil`
  - `mdutil -s /`
  - `mdutil -s /System/Volumes/Data`

## 3. 当前机器上的已确认事实

### 3.1 系统盘总体状态

当前内部数据卷状态：

- `system_profiler SPStorageDataType` 显示内部数据卷容量约 `245.11G`
- `diskutil apfs list` 最新查询显示 APFS 容器实际已使用约 `238.8G`
- 其中 `Data` 卷实际占用约 `213.9G`
- 其中 `System` 快照约 `12.5G`
- 其中 `Preboot` 卷约 `8.9G`
- 其中 `VM` 卷最新约 `2.1G`
- `df -h /System/Volumes/Data` 显示数据卷可用空间一度低到约 `1.2G`，最新查询约 `5.9Gi`
- `du -sh /Users/*` 显示当前用户目录约 `57G`
- Finder 递归统计 `~/` 显示当前用户目录实际约 `66.5G`

这说明问题确实在内部数据卷，不是外置盘或系统只读卷的错觉。

### 3.2 不是这些常见“背锅项”

本机已经排除的项目：

- `/private/var/vm` 为 `0B`
  - 这只说明旧路径下没有 swap；当前机器的 swap 实际在独立的 `VM` 卷中
- `tmutil listlocalsnapshots /`
  - 没有本地快照条目
- `diskutil apfs listSnapshots /System/Volumes/Data`
  - 返回 `No snapshots`
- `/private/var/log`、`~/Library/Logs`
  - 都很小，不是主因
- Xcode / CoreSimulator / iOS Backup / Docker 用户容器
  - 当前都不是大户

## 4. 当前可见的大户

### 4.1 用户库整体

可见范围里最大的单块是：

- Finder 递归统计 `~/Library` 约 `49.1G`
- 在为当前终端开启 `Full Disk Access` 后，`du -hd 1 ~/Library` 可直接看到约 `42G`

### 4.2 `~/Library/Application Support`

当前能看到的较大目录：

- `Code` 约 `1.7G`
- `Steam` 约 `1.7G`
- `Microsoft Edge` 约 `1.1G`
- `Zed` 约 `785M`
- `JetBrains` 约 `536M`
- `Microsoft` 约 `471M`
- `CodeBuddy CN` 约 `346M`
- `zoom.us` 约 `300M`
- `io.github.clash-verge-rev.clash-verge-rev` 约 `288M`
- `Epic` 约 `163M`

进一步下钻后，明显的大子目录包括：

- `Code/CachedExtensionVSIXs` 约 `703M`
- `Code/WebStorage` 约 `515M`
- `Code/User/workspaceStorage` 约 `263M`
- `Steam/Steam.AppBundle/Steam` 约 `1.2G`
- `Microsoft Edge/Default` 约 `691M`
- `Microsoft Edge/Default/Extensions` 约 `395M`
- `Zed/node` 约 `368M`
- `Zed/languages` 约 `276M`
- `JetBrains/PyCharm2025.1/plugins` 约 `530M`

### 4.3 `~/Library/Caches`

当前能看到的较大缓存目录：

- `JetBrains` 约 `2.4G`
- `ms-playwright` 约 `1.0G`
- `go-build` 约 `421M`
- `pypoetry` 约 `231M`
- `Microsoft Edge` 约 `172M`
- `ms-playwright-go` 约 `127M`
- `Homebrew` 约 `65M`

### 4.4 `~/Library/Group Containers`

这里不算很大，最大的可见项是：

- `UBF8T346G9.Office` 约 `241M`

### 4.5 其他可见位置

- `/private/var/folders` 约 `8.0G`
- `/private/var/db` 约 `2.8G`
- `~/.codex` 约 `3.5G`
- `~/Downloads` 约 `4.1G`
- `~/Desktop` 约 `1.5G`
- `~/Library/Parallels` 约 `4.6G`
- `~/Library/Developer` 当前仅约 `248K`

当前直接命中的大文件包括：

- `~/Library/Parallels/Downloads/26100.2033.241004-2336.ge_release_svc_refresh_CLIENTCONSUMER_RET_A64FRE_zh-cn.iso` 约 `4.6G`
- `~/.codex/logs_1.sqlite` 约 `1.1G`
- `~/.worldquant_control/accounts/asyu12-163-com-d90ba420/console.db` 约 `1.1G`
- `~/Desktop/录屏2026-03-27 15.07.19.mov` 约 `1.3G`

### 4.6 Finder 递归统计下的 `~/Library` 主体构成

这轮直接让 Finder 递归统计后，`~/Library` 的主体已经能比较清楚地拆开：

- `~/Library/Containers` 约 `27.6G`
- `~/Library/Application Support` 约 `8.3G`
- `~/Library/Caches` 约 `5.7G`
- `~/Library/Parallels` 约 `4.9G`
- `~/Library/Group Containers` 约 `350M`

判断：

用户目录里真正最重的一块，不是 `Downloads`，而是 `~/Library` 里被 Finder 平时弱化显示的容器、缓存和应用支持目录。

### 4.7 `~/Library/Containers` 里真正的大户

在给当前终端开启 `Full Disk Access` 后，`~/Library/Containers` 已经可以精确下钻。当前最大的几个容器是：

- `com.tencent.xinWeChat` 约 `14G`
- `com.kingsoft.wpsoffice.mac` 约 `3.0G`
- `com.apple.Safari` 约 `2.7G`
- `com.tencent.qq` 约 `2.5G`
- `com.kugou.mac.Music` 约 `515M`
- `com.tencent.meeting` 约 `308M`

进一步下钻后：

- `com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat` 约 `12G`
- `com.tencent.xinWeChat/Data/.wxapplet` 约 `1.0G`
  - `web` 约 `456M`
  - `packages` 约 `358M`
  - `WMPF` 约 `149M`
- `com.kingsoft.wpsoffice.mac/Data/.kingsoft` 约 `2.1G`
  - `wps/addons` 约 `1.4G`
  - `office6/data` 约 `640M`
  - 其中 `office6/data/backup` 约 `385M`
  - 其中 `office6/data/fonts` 约 `249M`
- `com.apple.Safari/Data/Library/WebKit/WebsiteData` 约 `2.7G`
  - 其中两个最大站点数据目录分别约 `2.0G` 与 `435M`
- `com.tencent.qq/Data/Library/Application Support/QQ` 约 `2.5G`
- `com.tencent.meeting/Data/Library/Global/Data` 约 `264M`

判断：

用户库里这次新确认的最大块并不是 Apple 自带 `Mail` 或 `Messages`，而是 WeChat、QQ、WPS Office 和 Safari 的容器数据。

### 4.8 其他已确认的大户

除了用户目录，还确认了这些对“系统数据”贡献明显的位置：

- `/System/Volumes/Data/usr/local` 约 `9.2G`
- `/System/Volumes/Data/opt/homebrew` 约 `6.6G`
- `/System/Volumes/Data/System/Library/AssetsV2` 约 `4.5G`
- `/private/var/db/diagnostics` 约 `1.6G`
  - `Special` 约 `974M`
  - `Persist` 约 `499M`
- `/private/var/db/uuidtext` 约 `675M`
- `/private/var/folders/hq` 约 `2.3G`
  - `C` 约 `1.3G`
  - `T` 约 `1.0G`

`AssetsV2` 进一步拆开后，较大的 Apple 资产目录包括：

- `com_apple_MobileAsset_UAF_Translation_Assets` 约 `1.2G`
- `com_apple_MobileAsset_Font8` 约 `1.1G`
- `com_apple_MobileAsset_UAF_Siri_Understanding` 约 `590M`
- `com_apple_MobileAsset_LinguisticData` 约 `488M`
- `com_apple_MobileAsset_UAF_Speech_AutomaticSpeechRecognition` 约 `230M`
- `com_apple_MobileAsset_UAF_Siri_TextToSpeech` 约 `199M`

### 4.9 APFS 卷本身也在吃空间

不能只盯目录树，因为 APFS 其他卷也占内部 SSD：

- `Preboot` 卷实际使用约 `8.9G`
- `VM` 卷最新实际使用约 `2.1G`
  - 该卷大小会随 swap 回收动态变化
- `System` 快照约 `12.5G`

这些空间不会直接出现在 `Macintosh HD` 根目录的一级可见目录相加里，但会真实占用 APFS 容器。

### 4.10 Finder 一级目录为什么经常只加出一部分

`Macintosh HD` 根目录视图只会让人直观看到：

- `系统`
- `应用程序`
- `用户`
- `资源库`
- `usr`
- `private` 的入口本身

但 `Storage` 面板统计的却是整个 APFS 数据卷，里面还包括：

- `~/Library` 下大量不在 Finder 默认视图里展开的缓存、容器和数据库
- `~/Library/Mail`、`~/Library/Messages`、`~/Library/Safari`、`~/Library/Application Support/CloudDocs` 这类受保护目录
- Apple 自带 `Containers` 与 `Group Containers`
- `/private/var` 下的缓存、索引和数据库
- 各类工具写在用户库里的镜像、日志和 sqlite 数据文件

所以“Finder 一级目录相加只有 100G 左右”与“Storage 显示已用 240G 左右”可以同时成立，它们本来就不是同一层统计口径。

## 5. 为什么还解释不完“100 多 G”

### 5.1 终端当前没有完全磁盘访问

这次扫描里，下面这类目录出现了大量 `Operation not permitted`：

- `~/Library/Mail`
- `~/Library/Messages`
- `~/Library/Safari`
- `~/Library/Application Support/CloudDocs`
- `~/Library/Application Support/Knowledge`
- 多个 Apple 自带 `Group Containers`
- 多个 Apple 自带 `Containers`

这意味着：

- 当前终端只能看到 `~/Library` 的一部分
- “系统数据”里相当一块 Apple 私有容器与数据库，现在只能确认存在盲区，不能精确点名到文件

在为当前终端开启 `Full Disk Access` 后，这个盲区已经缩小到主要剩下 Data 卷根部的隐藏系统目录；`~/Library` 下原本被拦住的 `Mail`、`Messages`、`Safari`、`CloudDocs` 已可直接读取，而且体积都不大。

### 5.2 Data 卷里仍有一大块隐藏系统目录没被直接量到

判断：

按当前证据，`Data` 卷当前实际约 `213.9G`。已经确认的顶层大户包括：

- `Users` 约 `66.6G`
- `Applications` 约 `27.3G`
- `Library` 约 `7.2G`
- `usr/local` 约 `9.2G`
- `opt/homebrew` 约 `6.6G`
- `private` 约 `5.3G`
- `System/Library/AssetsV2` 约 `4.5G`

但按 `du` 重新核算，当前能直接从 Data 根部读到的顶层目录只有约：

- `Users` 约 `58G`
- `Applications` 约 `15G`
- `Library` 约 `3.8G`
- `usr` 约 `9.2G`
- `opt` 约 `6.6G`
- `private` 约 `5.4G`
- `System` 约 `4.5G`

合计仅约 `102.5G`。

这意味着 Data 卷当前仍有大约 `100G` 以上，必须落在下面这些终端当前拿不到精确体积的路径或 APFS 目录结构里：

- `/System/Volumes/Data/.Spotlight-V100`
- `/System/Volumes/Data/.DocumentRevisions-V100`
- `/System/Volumes/Data/.fseventsd`
- 其他 Apple 受保护或 root-only 的 Data 卷隐藏目录

这是这次排查里最大的剩余盲区。

当前已验证的权限边界是：

- `Full Disk Access` 足以读取大多数 `~/Library` 受保护目录
- 但对 `/System/Volumes/Data/.Spotlight-V100`、`/.DocumentRevisions-V100`、`/.fseventsd` 仍然会返回 `Permission denied`
- `sudo -n` 返回 `a password is required`
- `mdutil -s /` 与 `mdutil -s /System/Volumes/Data` 都返回 `Index is read-only`

因此，继续精确量这部分隐藏根目录，需要用户本机以管理员权限手动执行 `sudo du ...`

### 5.3 `mdutil -d` / `-X` 的真实语义和适用边界

本机 `mdutil` 帮助和手册确认：

- `-d`
  - Disable Spotlight activity for volume
- `-E`
  - Erase and rebuild index
- `-X`
  - Remove the Spotlight index directory on the specified volume
  - Does not disable indexing
  - Spotlight may recreate it after remount, reboot, or显式索引命令

判断：

- 文章里 `mdutil -d "/Volumes/NO NAME"` + `mdutil -X "/Volumes/NO NAME"` 的写法，更适合外置盘或普通可管理卷
- 对当前这台 APFS 系统盘，`/` 与 `/System/Volumes/Data` 都显示 `Index is read-only`
- 因此不能直接把外置盘的处理方式等同于系统盘，也不能先假设 `.Spotlight-V100` 一定能靠 `mdutil -X` 在当前系统环境下直接清掉

### 5.4 `du`、Finder 和 `diskutil` 在 APFS 上不能混着当真相

判断：

- `diskutil apfs list` / `diskutil info` 更接近卷级真实占用
- `du` 适合看目录热点，但在 APFS 某些卷上会被重复引用或 cryptex 结构误导
  - 例如 `du -sh /System/Volumes/Preboot` 可见约 `27G`
  - 但 `diskutil info /System/Volumes/Preboot` 的真实卷使用约 `8.9G`
- Finder 的 `size` 属性对常规用户目录很有用，但对 `VM`、root 隐藏目录或某些系统路径会返回 `missing value` 或明显偏低

因此：

1. 卷大小优先看 `diskutil`
2. 普通目录热点优先看 `du`
3. 受保护用户目录可用 Finder 递归统计补盲

## 6. 当前最有价值的结论

按现有证据，这台机器上最值得优先怀疑的来源是：

1. `~/Library` 里的受保护 Apple 容器与数据库
2. `~/Library/Containers` 这块单独就有约 `27.6G`
3. 其中最大的容器明确是 `WeChat 14G`、`WPS Office 3.0G`、`Safari 2.7G`、`QQ 2.5G`
4. `~/Library/Caches` 与 `~/Library/Application Support`
5. `/System/Volumes/Data/usr/local` 与 `/System/Volumes/Data/opt/homebrew`
6. `/System/Volumes/Data/System/Library/AssetsV2`
7. `/private/var/folders` 与 `/private/var/db/diagnostics`
8. Data 卷根部仍未拿到精确数字的隐藏系统目录

不太可能是：

1. swap
2. 本地快照
3. iOS 备份
4. Xcode 模拟器

## 7. 推荐下一步

### 7.1 如果要拿到精确文件级答案

给当前终端或你实际运行 Codex 的宿主应用开启：

- `System Settings -> Privacy & Security -> Full Disk Access`

然后重跑这些命令：

```bash
sudo du -sh /System/Volumes/Data/.Spotlight-V100 /System/Volumes/Data/.DocumentRevisions-V100 /System/Volumes/Data/.fseventsd
du -sh ~/Library/Mail ~/Library/Messages ~/Library/Safari
du -hd 1 ~/Library 2>/dev/null | sort -h | tail -40
find ~/Library/Containers -mindepth 1 -maxdepth 1 -type d -exec du -sh {} + 2>/dev/null | sort -h | tail -80
find ~/Library/Group\ Containers -mindepth 1 -maxdepth 1 -type d -exec du -sh {} + 2>/dev/null | sort -h | tail -80
```

### 7.2 如果只是先做安全清理

优先级建议：

1. `~/Library/Caches/JetBrains`
2. `~/Library/Caches/ms-playwright`
3. `~/Library/Application Support/Code/CachedExtensionVSIXs`
4. `~/Library/Application Support/Code/WebStorage`
5. `~/.codex/sessions`
6. `~/Downloads`

这些目录相对可控，而且当前证据充分。

## 8. 相关文档

- `README.md`
- `colima-external-storage.md`
