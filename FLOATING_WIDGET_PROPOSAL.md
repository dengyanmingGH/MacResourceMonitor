# MacResourceMonitor 功能拓展规划报告

> 分析日期：2026-10-18  
> 目标项目：/Users/dengyanming/Work/DshWork/MacResourceMonitorFixed（Python + rumps + PyObjC 方案，已稳定运行）  
> 参考项目：/Users/dengyanming/Work/DshWork/MacResourceMonitor（Swift 原型，仅作技术参考，未采用）

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

## 2. 按键音效 / Tikey 类功能分析

### 2.1 Tikey 功能理解

Tikey 主要提供系统键盘音的更细粒度控制（音量调节、独立开关等）。macOS 原生只提供「Sound Effects」中的粗粒度设置。

### 2.2 实现路径分析

| 路径 | 沙箱可用 | 非沙箱可用 | 权限 | 实现复杂度 | 说明 |
|---|---|---|---|---|---|
| defaults 读写（com.apple.sound.beepVolume） | 是 | 是 | 无 | 极低 | 只覆盖「系统 beep 音量」子集 |
| Accessibility + CGEventTap | 否 | 是 | Input Monitoring / Accessibility（用户授权） | 高 | 可实现全量 Tikey（监听键盘、叠加自定义音效） |
| IOHID | 否 | 是 | 系统级权限 | 高 | 底层但实现最繁琐 |
| AppleScript/System Settings | 是 | 是 | 无 | 中 | 功能极度受限（仅能模拟用户操作） |

### 2.3 Swift vs Python+PyObjC

| 维度 | Swift | Python + PyObjC |
|---|---|---|
| CGEventTap | 原生、回调自然 | 可用但需 NSObject 子类桥接（类似 _SegmentAction） |
| defaults 操作 | Foundation.NSUserDefaults | subprocess + defaults 或 plistlib |
| IOKit | 原生绑定完整 | PyObjC 覆盖较不完整 |
| 与现有采集层集成 | 需 IPC | 直接复用 Python 逻辑 |
| 权限弹窗 UX | 最顺滑 | 可实现但略繁琐 |

### 2.4 建议集成方式

采用「独立模块 + 渐进式」策略：

- `float_window.py` — 浮窗实现
- `key_sound.py` — 按键音效设置（Phase 1 先做 defaults 子集）

## 3. 可行性总结 + 下一步行动清单

### 3.1 三个短期原型

| 原型 | 内容 | 代码量 | 风险 | 验证方式 |
|---|---|---|---|---|
| **原型 1（最优先）** | NSPanel 浮窗 MVP：220×120 尺寸、CPU+内存双指标、小曲线、可拖拽、位置记忆 | ~150 行 | 中低（z-order 行为需实测） | 新增 FloatWindow 类 + 菜单切换，构建后实机测试 |
| **原型 2** | Liquid Glass 增强：NSGlassEffectView、尺寸可调、指标切换、右键菜单 | ~100 行（增量） | 低（macOS 26 SDK 可用性） | 在原型 1 基础上增量开发 |
| **原型 3（按键音效）** | beep 音量控制：defaults 读写 com.apple.sound.beepVolume，菜单中滑块调节 | ~80 行 | 极低 | 独立脚本验证键值有效后集成 |

### 3.2 务实建议

- **不要急于 Swift 重写。** 当前 Python + rumps + PyObjC 架构已稳定，浮窗功能在该架构内完全可行且维护成本最低。
- **浮窗优先。** 用户感知最强、复用现有 UI 组件最多、技术风险可控。
- **按键音效分阶段做。** 先做 Phase 1（defaults 子集），无需任何授权即有价值；完整 Tikey（CGEventTap）放到后期再评估是否真的必要。
- **单独文件新增。** 保持 `system_monitor.py` 主文件规模可控，便于后续测试和维护。

### 3.3 下一步行动清单

1. **[Day 1]** 在 `system_monitor.py` 附近新增 `float_window.py`，实现 `FloatWindow` 基础骨架（创建 NSPanel、显隐、拖拽）
2. **[Day 1-2]** 在 `SystemMonitorApp` 集成浮窗：菜单项、配置持久化、定时刷新
3. **[Day 2]** 实机测试：置顶行为、与菜单栏联动、位置记忆是否正常
4. **[Day 3]（如需）** 增加点击穿透开关、尺寸可调、Liquid Glass
5. **[可选]** 按键音效原型：新增 `key_sound.py` + 菜单子项，实现 beepVolume 滑块
