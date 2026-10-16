# 系统资源监控 (MacResourceMonitor)

macOS 菜单栏系统资源监控工具。

## 快速使用（在访达中双击）

| 操作 | 文件 |
|------|------|
| **启动监控** | `01-启动监控.command` |
| **停止监控** | `02-停止监控.command` |
| **开启开机自启** | `09-安装开机自启.command` |
| **关闭开机自启** | `10-卸载开机自启.command` |

也可以直接双击 `dist/MacResourceMonitor.app`。

## 菜单栏显示

菜单栏右上角默认显示 **内存使用占比**（如 `69%`）。

点击菜单可以在「**菜单栏显示**」子菜单里实时切换（无需改代码、无需重新构建）：
内存 / CPU / 磁盘 / GPU / CPU 温度 / 风扇 / 下载速度 / 电池。

设置会持久化到：

```
~/Library/Application Support/MacResourceMonitor/config.json
```

点击它可以查看全部指标；选择「显示详情…」可打开详细数据面板
（含历史曲线、会话峰值、Top5 进程）。

## 开机自启

已通过 LaunchAgent 配置，登录后自动启动；若进程意外退出会自动重启。

- 配置位置：`~/Library/LaunchAgents/com.dengyanming.macresourcemonitor.plist`
- 项目模板：[autostart.plist](autostart.plist)
- 关闭方式：双击 `10-卸载开机自启.command`

### 为什么必须用 C 启动器

`.app` 的主可执行文件是编译好的 `launcher.c`，它 **fork** 出子进程运行
Python，父进程保持存活。这是必需的：

macOS 26 的状态栏项由 ControlCenter 以 scene 方式托管。如果启动器用
`exec()` 直接把进程替换成 Python，macOS 无法把该进程与应用包身份关联，
ControlCenter 会拒绝创建状态栏场景：

```
[AppKit:StatusBar] scene activation failed:
BSServiceConnectionErrorDomain Code=3 "XPC error received on message reply handler"
```

结果是进程在运行但菜单栏**没有任何图标**。用 fork 保留父进程身份后即正常。
（这一坑在终端直接运行 Python 时不会出现，因为那种情况下不涉及应用包身份匹配。）

## 运行时设置（菜单中实时调整）

| 菜单 | 说明 |
|------|------|
| 菜单栏显示 | 菜单栏标题指标（内存/CPU/磁盘/GPU/温度/风扇/下载/电池） |
| GPU 数据采样 | powermetrics 采样间隔：关闭 / 2s / 5s / 8s / 15s |
| 温度告警 | 告警总开关 + 阈值（85/90/95/100°C） |
| 立即刷新 GPU/CPU 数据 | 手动触发一次 powermetrics 采样 |
| 数据源 | 实时显示 iSMC / powermetrics 健康状态 |

## 告警

- CPU 核心温度超过阈值（默认 90°C）时发送系统通知
- 热压力等级非 Nominal（Moderate/Heavy/Critical）时发送系统通知
- 同类告警 5 分钟冷却，避免刷屏；可在「温度告警」菜单关闭

## 监控指标

| 指标 | 数据来源 | 是否需要权限 |
|------|----------|--------------|
| CPU 使用率 / 每核占用 | psutil | 否 |
| 内存使用（含活跃/wired） | psutil | 否 |
| Swap 使用 | psutil | 否 |
| 磁盘使用（APFS 容器口径） | psutil | 否 |
| 磁盘 I/O 读/写速率 | psutil | 否 |
| 网络速率 | psutil | 否 |
| 电池电量 | psutil | 否 |
| Top5 进程（CPU/内存） | psutil | 否 |
| 历史曲线与峰值（CPU/内存/温度） | 本地记录 | 否 |
| **CPU 核心温度**（平均/最高） | iSMC | 否 |
| **GPU 散热器温度**（⚠️ 非核心温度，见下） | iSMC | 否 |
| **CPU 热管温度 / 无线网卡温度** | iSMC | 否 |
| **SSD / 电池温度** | iSMC | 否 |
| **风扇转速**（当前/上限） | iSMC | 否 |
| GPU 使用率 / 频率 / 功率 | powermetrics | 免密 sudo（已配置） |
| CPU P核/E核频率 | powermetrics | 免密 sudo（已配置） |
| CPU 功率 / 热压力等级 | powermetrics | 免密 sudo（已配置） |

## ⚠️ 两处数据口径说明（重要）

### 1. 磁盘使用率为何是 71% 而不是 15%

macOS 的 `/` 是**只读系统卷**，只含约 12GB 系统文件；用户数据在
`/System/Volumes/Data`。如果直接对 `/` 求使用率，会得到严重偏低的假数据
（实际 71% 会显示成 15%）。

正确做法：这两个卷共享同一个 APFS 容器，对数据卷调用 `statvfs` 时
`f_blocks` 返回整个容器大小、`f_bavail` 是容器可用空间，相减即为真实已用量。
本项目采用该口径：

```
已用 162 GB / 总 228 GB = 71%，可用 66 GB
```

（与 `diskutil info` 的 Container Total/Free Space 一致）

### 2. 为什么不再显示「GPU 温度」

在 MacBookPro17,1 (M1) 上用 macmon 交叉验证发现：

```
--- SMC temp sensors ---
Tg1b=00.0  Tg4b=00.0                              ← GPU 核心温度传感器读数为 0
TCMb=73.0  TCMz=84.5                              ← CPU 核心温度（真实）
GPU Stats :: Temperature :: Average Sum () = 0
```

- **CPU 核心温度真实有效**：macmon 的 `cpu_temp_avg` 与 iSMC 的 `CPU Die Average` 一致
- **GPU 核心温度本机不可用**：传感器读数为 0，macmon 也返回 `gpu_temp_avg = None`
- 早先显示的「GPU 温度 32°C」其实是 **GPU 散热器/板级探针 (TG0H)**，位于环境温度附近，
  与 CPU 核心结温（60~80°C）**物理量纲不同，不可直接比较**

因此界面现在明确标注为「GPU 散热器」，并注明核心温度本机不可用，避免误解。

## 已配置的系统权限

`/etc/sudoers.d/macmon` 已添加免密规则，使应用无需输入密码即可读取
GPU / 频率 / 功率数据：

```
dengyanming ALL=(ALL) NOPASSWD: /usr/bin/powermetrics
```

如需撤销，执行：`sudo rm /etc/sudoers.d/macmon`

## 依赖

- Python 3.10+（构建时自动探测，Homebrew 优先）
- rumps、psutil（构建时装进 `.app/Contents/Resources/lib`）
- iSMC（`/opt/homebrew/bin/iSMC`，通过 `brew install dkorunic/tap/ismc` 安装）
- powermetrics（macOS 系统自带）

## 文件说明

```
MacResourceMonitorFixed/
├── 01-启动监控.command              # 双击启动
├── 02-停止监控.command              # 双击停止
├── 09-安装开机自启.command          # 双击开启自启
├── 10-卸载开机自启.command          # 双击关闭自启
├── autostart.plist                  # LaunchAgent 模板
├── launcher.c                       # C 启动器源码（关键！）
├── build.sh                         # 一键构建脚本
├── system_monitor.py                # 主程序
├── build/lib/                       # pip 依赖缓存（增量复用，加速重建）
└── dist/
    └── MacResourceMonitor.app       # macOS 应用包
```

## 配置与日志

```
~/Library/Application Support/MacResourceMonitor/config.json   # 运行时设置
/tmp/macresourcemonitor.log                                    # 运行日志
```

## 修改代码后重新构建

```bash
cd /Users/dengyanming/Work/DshWork/MacResourceMonitorFixed
./build.sh                 # 自动探测 Python，依赖增量缓存
launchctl kickstart -k gui/$(id -u)/com.dengyanming.macresourcemonitor   # 重启
```

构建脚本会：

- 自动查找可用的 Python 解释器（Homebrew 3.10~3.13 优先），
  写入 `.app/Contents/Resources/interpreter`；启动器优先按此路径启动，
  失败时依次回退 `/opt/homebrew/bin/python3.10` … `/usr/bin/python3`
- 依赖安装到 `build/lib` 缓存，未变化时跳过 pip
- 重新编译 C 启动器并 ad-hoc 签名
