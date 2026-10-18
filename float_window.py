"""
MacResourceMonitor - 桌面浮窗 / 小组件
=======================================
方案：PyObjC NSPanel（borderless + nonactivating + floating panel），与菜单栏
主程序同属一进程，复用 system_monitor 的 ChartView / 颜色表。

能力：
  - 始终置顶（NSStatusWindowLevel）
  - 可拖拽（setMovableByWindowBackground_）
  - 半透明背景、圆角、阴影
  - 点击穿透（可选）
  - 位置 + 尺寸记忆（config.json）
  - 指标切换（CPU/内存/磁盘/网络/GPU/温度/风扇/电池），点击切换
  - 当前值 + 迷你曲线
"""

import time

import objc
from Foundation import NSObject, NSMakeRect
from AppKit import (
    NSView, NSButton, NSButtonCell, NSPanel, NSApplication, NSColor, NSFont,
    NSTextField, NSSlider,
    NSBackingStoreBuffered, NSStatusWindowLevel, NSFloatingWindowLevel,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
)

# 浮窗缺省尺寸
FW_W_DEFAULT = 230
FW_H_DEFAULT = 110

# 浮窗显示指标。曲线从快照 hist 中读取同名 key 的历史序列。
FW_METRICS = [
    ("cpu", "CPU"),
    ("mem", "内存"),
    ("disk", "磁盘"),
    ("net", "网络"),
    ("gpu", "GPU"),
    ("temp", "温度"),
    ("fan", "风扇"),
    ("battery", "电池"),
]


class _FloatClickHandler(NSObject):
    """点击面板背景 → 切换当前指标。"""

    def initWithFloat_(self, fw):
        self = self.init()
        if self is None:
            return None
        self._fw = fw
        return self

    def mouseDown_(self, event):
        self._fw._next_metric()


class FloatWindow:
    def __init__(self, app):
        self._app = app
        self.panel = None
        self.graph = None        # ChartView
        self.legend = None       # 当前值
        self.icon_lab = None     # 指标名
        self._created = False
        self._metric_idx = 0
        self._hidden = True
        self._graph_color = None

    # ---------- 构建 ----------

    def _create(self):
        from system_monitor import ChartView, GRAPH_COLORS, _sys_color

        cfg = self._app.config if self._app is not None else {}
        W = int(cfg.get("float_window_w", FW_W_DEFAULT))
        H = int(cfg.get("float_window_h", FW_H_DEFAULT))

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)

        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setWorksWhenModal_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setMovableByWindowBackground_(True)
        panel.setOpaque_(False)
        panel.setHasShadow_(True)
        # 窗口级别决定是否始终置顶；点击穿透与「点击切换指标」互斥，
        # 此处默认关闭点击穿透（便于点击面板切换指标），通过菜单/配置可选开启。
        panel.setIgnoresMouseEvents_(False)
        panel.setBackgroundColor_(
            NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.88))

        # 置顶层级
        level = NSStatusWindowLevel if cfg.get("float_window_topmost", True) \
            else NSFloatingWindowLevel
        panel.setLevel_(level)

        content = panel.contentView()
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(cfg.get("float_window_corner", 12.0))
        content.layer().setMasksToBounds_(True)

        # 点击背景 → 切换当前指标
        clicker = _FloatClickHandler.alloc().initWithFloat_(self)
        bg = NSButton.buttonWithTitle_target_action_("", clicker, "mouseDown:")
        bg.setFrame_(NSMakeRect(0, 0, W, H))
        bg.setBordered_(False)
        bg.setWantsLayer_(True)
        bg.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        content.addSubview_(bg)  # NSNoResponder

        margin = 10
        # 左侧指标名
        self.icon_lab = NSTextField.alloc().initWithFrame_(
            NSMakeRect(margin, H - 22, 80, 16))
        self.icon_lab.setBezeled_(False)
        self.icon_lab.setDrawsBackground_(False)
        self.icon_lab.setEditable_(False)
        self.icon_lab.setSelectable_(False)
        self.icon_lab.setFont_(NSFont.systemFontOfSize_weight_(13, 0.0))
        self.icon_lab.setTextColor_(NSColor.labelColor())
        content.addSubview_(self.icon_lab)

        # 右下当前值
        self.legend = NSTextField.alloc().initWithFrame_(
            NSMakeRect(margin, 8, W - margin * 2, 16))
        self.legend.setBezeled_(False)
        self.legend.setDrawsBackground_(False)
        self.legend.setEditable_(False)
        self.legend.setSelectable_(False)
        self.legend.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(13, 0.0))
        self.legend.setTextColor_(NSColor.labelColor())
        self.legend.setAlignment_(2)  # right
        content.addSubview_(self.legend)

        # 迷你曲线
        gx = margin + 80 + 6
        gw = W - gx - margin
        gy = H - 18 - 22
        self.graph = ChartView.alloc().initWithFrame_(NSMakeRect(gx, gy, gw, 22))
        content.addSubview_(self.graph)

        self._apply_metric_label()

        # 位置记忆（启动时恢复）
        x = cfg.get("float_window_x")
        y = cfg.get("float_window_y")
        if x is not None and y is not None:
            try:
                panel.setFrame_display_(NSMakeRect(float(x), float(y), W, H), False)
            except Exception:
                pass

        self.panel = panel
        self._created = True

    # ---------- NSWindowDelegate：记忆位置（在 hide 时落盘） ----------

    def _save_position(self):
        try:
            if not self.panel:
                return
            f = self.panel.frame()
            cfg = self._app.config if self._app is not None else {}
            cfg["float_window_x"] = f.origin.x
            cfg["float_window_y"] = f.origin.y
            if self._app is not None and hasattr(self._app, "save_config"):
                self._app.save_config(cfg)
        except Exception as e:
            import sys
            print("float win move error:", e, file=sys.stderr)

    # ---------- 指标 ----------

    def _metric_key(self):
        return FW_METRICS[self._metric_idx % len(FW_METRICS)][0]

    def _apply_metric_label(self):
        from system_monitor import GRAPH_COLORS, _sys_color
        key = self._metric_key()
        self.icon_lab.setStringValue_(dict(FW_METRICS).get(key, key))
        self._graph_color = _sys_color(GRAPH_COLORS.get(key, "systemBlueColor"))

    def _next_metric(self):
        self._metric_idx = (self._metric_idx + 1) % len(FW_METRICS)
        self._apply_metric_label()

    # ---------- 显隐 ----------

    def show(self):
        if not self._created:
            self._create()
        self._hidden = False
        self.panel.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def hide(self):
        if self.panel:
            self._save_position()
            self.panel.orderOut_(None)
        self._hidden = True

    def is_visible(self):
        return self._created and self.panel is not None and self.panel.isVisible()

    # ---------- 数据刷新 ----------

    def update(self, s):
        if not self._created:
            return
        try:
            key = self._metric_key()
            hist = s.get("hist") or {}
            data = hist.get(key) or []
            self.graph.setData_color_threshold_(data, self._graph_color, None)
            self.legend.setStringValue_(_fmt_value(s, key))
        except Exception as e:
            print("float window update error:", e, file=__import__("sys").stderr)


def _last(s, *keys):
    """取快照嵌套路径中最后一个标量值。"""
    cur = s
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif cur is None:
            return None
        else:
            return None
    if isinstance(cur, list):
        vals = [v for v in cur if isinstance(v, (int, float))]
        return vals[-1] if vals else None
    return cur if isinstance(cur, (int, float)) else None


def _fmt_value(s, key):
    """格式化浮窗当前值。"""
    if key == "cpu":
        return _pct(_last(s, "hist", "cpu"))
    if key == "mem":
        return _pct(_last(s, "hist", "mem"))
    if key == "disk":
        return _pct(s.get("disk_percent"))
    if key == "net":
        v = s.get("net_down_mb")
        return "--" if v is None else "{:.1f} MB/s".format(v)
    if key == "gpu":
        return _pct((s.get("pm") or {}).get("gpu_usage"))
    if key == "temp":
        t = s.get("temps") or {}
        return _pct(t.get("cpu_avg") if isinstance(t.get("cpu_avg"), (int, float)) else t.get("max"))
    if key == "fan":
        v = s.get("fan_current")
        return "--" if v is None else "{:.0f}".format(v)
    if key == "battery":
        return _pct(s.get("battery_percent"))
    return "--"


def _pct(v):
    if v is None:
        return "--"
    return "{:.0f}%".format(v)


# ============================================================
# 声音设置滑动面板（Tikey 类 UI）
# ============================================================

class SoundPanel:
    """一个极小的悬浮面板，嵌入 NSSlider 连续控制提示音音量。

    复用 key_sound 模块（osascript + defaults），无额外权限。
    面板本身不是必须的 —— 菜单离散分档已足够 MVP —— 但它给出
    与 Tikey 一致的「拖动滑块即时调出系统提示音」体验。
    """

    W, H = 230, 110

    def __init__(self, app):
        self._app = app
        self.panel = None
        self.slider = None
        self.pct_label = None
        self.beep_btn = None
        self.flash_btn = None

    def _create(self):
        from AppKit import (NSView, NSColor, NSFont, NSPanel, NSTextField,
                            NSSlider, NSButton, NSWindowStyleMaskBorderless,
                            NSWindowStyleMaskNonactivatingPanel,
                            NSBackingStoreBuffered, NSStatusWindowLevel)

        if self.panel is not None:
            return
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.W, self.H),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(
            NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.88))
        panel.setHasShadow_(True)
        panel.setLevel_(NSStatusWindowLevel)

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, self.W, self.H))
        view.setWantsLayer_(True)

        # 标题
        title = NSTextField.labelWithStringValue_withFrame_(
            "提示音设置", NSMakeRect(12, self.H - 22, self.W - 24, 17))
        title.setFont_(NSFont.boldSystemFontOfSize_(12))
        view.addSubview_(title)

        # 百分比标签
        self.pct_label = NSTextField.labelWithStringValue_withFrame_(
            "—", NSMakeRect(12, self.H - 48, 60, 15))
        self.pct_label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0))
        view.addSubview_(self.pct_label)

        # 滑块
        self.slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(72, self.H - 46, self.W - 84, 18))
        self.slider.setNumberOfTickMarks_(11)
        self.slider.setTickMarkPosition_(1)  # linear
        self.slider.setAllowsContinuous_(True)
        self.slider.setTarget_(self)
        self.slider.setAction_("sliderChanged:")
        view.addSubview_(self.slider)

        # 开启界面声音效果
        self.beep_btn = NSButton.buttonWithTitle_target_action_(
            "界面声音效果", self, "toggleBeep:")
        self.beep_btn.setFrame_(NSMakeRect(12, 18, 100, 24))
        self.beep_btn.setBezelStyle_(2)  # push
        view.addSubview_(self.beep_btn)

        # 闪屏
        self.flash_btn = NSButton.buttonWithTitle_target_action_(
            "闪屏提示", self, "toggleFlash:")
        self.flash_btn.setFrame_(NSMakeRect(120, 18, 100, 24))
        self.flash_btn.setBezelStyle_(2)
        view.addSubview_(self.flash_btn)

        panel.setContentView_(view)
        self.panel = panel
        self._sync_ui()

    def sliderChanged_(self, sender):
        pct = int(sender.doubleValue())
        self._app._ks.set_alert_volume(pct / 100.0)
        self._sync_ui()

    def toggleBeep_(self, sender):
        ks = self._app._ks
        ks.set_ui_sounds(not ks.ui_sounds_enabled())
        self._sync_ui()

    def toggleFlash_(self, sender):
        ks = self._app._ks
        ks.set_flash_when_beep(not ks.flash_when_beep())
        self._sync_ui()

    def _sync_ui(self):
        ks = self._app._ks
        pct = int(round(ks.alert_volume() * 100))
        if self.slider is not None:
            self.slider.setDoubleValue_(pct)
        if self.pct_label is not None:
            self.pct_label.setStringValue_("{}%".format(pct))
        if self.beep_btn is not None:
            self.beep_btn.setState_(1 if ks.ui_sounds_enabled() else 0)
        if self.flash_btn is not None:
            self.flash_btn.setState_(1 if ks.flash_when_beep() else 0)

    def show(self):
        if self.panel is None:
            self._create()
        self._sync_ui()
        self.panel.makeKeyAndOrderFront_(None)

    def hide(self):
        if self.panel is not None:
            self.panel.orderOut_(None)
