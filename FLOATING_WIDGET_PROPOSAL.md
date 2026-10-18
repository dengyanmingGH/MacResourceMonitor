# MacResourceMonitor 功能拓展规划报告

> 分析日期：2026-10-18
> 目标项目：/Users/dengyanming/Work/DshWork/MacResourceMonitorFixed（Python + rumps + PyObjC 方案，已稳定运行）

## 1. 浮窗 / 小组件（iStat Menus 风格）方案分析

## 1. 浮窗 / 小组件（iStat Menus 风格）方案分析

### 1.1 现状回顾

当前 `DetailsWindow`（system_monitor.py:830）已经是一个 NSWindow 实现的详情面板，具备：

- NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskFullSizeContentView
- win.setLevel_(NSFloatingWindowLevel + 1)
- 半透明、可拖动（setMovableByWindowBackground_）
- 自绘 CardView / ChartView / 分段控件等 UI 组件

这为从详情面板升级到「始终置顶迷你浮窗」打下了坚实基础。

### 1.2 三条实现路径对比

| 维度 | 方案 A：PyObjC NSPanel（推荐） | 方案 B：Swift 重写 | 方案 C：rumps + 额外 NSWindow |
|---|---|---|---|
| 原生体验 | 高（NSPanel 浮窗行为最接近 iStat Menus） | 最高（SwiftUI/AppKit + Liquid Glass） | 中 |
| 性能 | 优秀（与现有架构同构） | 优秀（原生编译） | 优秀 |
| 维护成本 | 低（单一语言，同一采集层） | 高（双语言、双构建链） | 中 |
| 打包方式 | 与现有 build.sh 完全兼容 | 需新增 Swift target | 与方案 A 接近 |
| 与菜单栏联动 | 直接（同一 SystemMonitorApp 进程） | 需 IPC | 直接 |
| Liquid Glass（macOS 26） | 可用（NSGlassEffectView） | 原生支持 | 可用 |
| 工作量预估 | ~150 行新代码 | ~800-1000 行及大幅改动 | ~150 行 |

### 1.3 推荐方案：方案 A — NSPanel

#### MVP 思路

新增 `FloatWindow` 类（与 `DetailsWindow` 平行），复用现有自绘组件。窗口改用 `NSPanel`，关键配置：

- `NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel`
- `setFloatingPanel_(True)`、`setBecomesKeyOnlyIfNeeded_(True)`
- `setLevel_(NSStatusWindowLevel)`（始终置顶）
- `setMovableByWindowBackground_(True)`（拖拽）
- `setOpaque_(False)`、`setHasShadow_(True)`（半透明+阴影）
- `setAllowsBackgroundWindowEvents_(True)` / `setIgnoresMouseEvents_(True)`（点击穿透开关）
- `setDelegate_` + `windowDidMove_`（位置记忆）
- 可选 `NSGlassEffectView`（macOS 26 Liquid Glass）

#### 集成点

1. `SystemMonitorApp.__init__`：实例化 `self.floatwin = FloatWindow()`
2. `refresh()` 回调：若浮窗可见则调用 `self.floatwin.update(s)`（复用 `collector.get()` 的快照）
3. 菜单新增 `显示浮窗` 项：`toggle_floatwin()` 切换显隐并打勾
4. 配置新增：`float_window_enabled`、`float_window_x/y`、`float_window_w/h`、`float_window_metric`

## 2. 可行性总结 + 下一步行动清单

- **不要 Swift 重写**：当前 Python + rumps + PyObjC 架构已稳定，浮窗功能在该架构内完全可行，维护成本最低。
- **浮窗已实现并实机验证**：`float_window.py` 提供 `FloatWindow` 类；`_create` 通过 `sys.modules["__main__"]` 取 `ChartView / GRAPH_COLORS / _sys_color`，规避因 `SystemMonitorApp.__init__` 顶层 `import float_window` 而引发的循环 import + `objc overriding` 错误；`NSPanel` 构建、`update(snapshot)` 渲染、点击切换指标、位置记忆、拖动均在 bundle 环境下验证通过。
- **声音设置：已移除**。依照要求（2026-10-18），项目不再涉及声音/按键音效功能；`key_sound.py`、`SoundPanel` 类及菜单项均已删除回退。

### 下一步行动清单

1. **[Day 1]** `float_window.py` + `SystemMonitorApp` 集成菜单项 `显示浮窗` + 配置持久化 — 已完成
2. **[Day 2]** 实机测试：置顶行为、菜单栏联动、位置记忆 — 已验证
3. **[如需]** 点击穿透开关、尺寸可调、Liquid Glass — 可选增量
