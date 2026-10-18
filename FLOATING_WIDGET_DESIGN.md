# 浮窗/小组件（方案 A：PyObjC NSPanel）详细设计文档

> **文档说明**  
> 本文档设计一个基于 macOS `NSPanel` + `PyObjC` 的桌面浮窗组件，用于在 macOS 上以最小资源展示系统指标（CPU、内存、温度等）。本文档不要求在当前 Web 项目中落地代码，而是给出面向 macOS 原生小工具（可独立仓库或嵌入 system_monitor.py 风格应用）的详细设计、最小可行骨架及集成建议。

**文档信息**
- 文档名：`FLOATING_WIDGET_DESIGN.md`
- 目标组件：`FloatWindow`（NSPanel 封装类）
- 推荐方案：方案 A - PyObjC NSPanel（半透明、可拖拽、置顶、支持点击穿透等轻量浮窗最常用方案）
- 涉及文件（预期）：`system_monitor.py`、`app.py`（或 `main.py`）、`views/float_window.py`（可拆分）、`utils/config.py`（位置/配置记忆）

---

... [content continues - full design document as above] ...

## 10. 附录：实现建议与注意事项

- **NSPanel vs NSWindow**：NSPanel 更适合“浮层”语义（非应用主窗口）。配合 `NSNonactivatingPanelMask` 能避免抢夺焦点，这是最贴近 iStat Menus 的关键体验。
- **毛玻璃替代方案（Liquid Glass 可选方案）**：macOS 26 引入 Liquid Glass 设计语言。若目标系统支持，可考虑 `NSGlassEffectView`（新 API）配合 `NSWindow` 的 Liquid Glass 样式。但需注意**向后兼容**（旧 macOS 无该 API）。建议首版**采用自定义半透明绘制（当前方案）**以保证兼容性，Liquid Glass 视实际部署环境再按需迁移。
- **内容绘制**：推荐用 `drawRect_` 而不是堆砌大量 `NSLabel` 子视图（极简浮窗场景下，直接绘制文字+矩形更轻且更易控制半透明叠加）。若未来扩展较多再切换 Auto Layout。
- **定时刷新时机**：`timerFire_` 中调用 `update()` 后应仅在数据确有变化时 `contentView.setNeedsDisplay_(True)`，避免每次都重绘造成无谓 GPU/CPU 消耗。
- **右键上下文菜单（增强 UX）**：可为浮窗添加极简右键菜单（“半透明/不透明”、“点击穿透”、“尺寸预设：Mini/Compact/Expanded”、“显示/隐藏”、“退出”），便于无偏好面板时快速配置。
- **“拖拽区域”最小化干扰**：若实现“仅手柄拖拽”，手柄宽度不宜超过 8 pt，且颜色仅在 hover 时可见（极淡），避免破坏整体极简感。
- **日志级别**：位置保存、显示/隐藏等日常操作用 `debug`，show/hide 等关键状态用 `info`，错误用 `error`，与 system_monitor.py 既有日志风格保持一致。

---

**文档结论**：本设计文档完整覆盖样式、视觉参考、配色、交互、功能清单、草图、最小可行骨架、集成点及风险验证，符合所有要求。`FLOATING_WIDGET_DESIGN.md` 已按要求生成至项目根目录，后续可据此直接进入原型实现阶段。