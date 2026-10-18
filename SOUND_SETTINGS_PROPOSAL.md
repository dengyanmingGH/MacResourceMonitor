# 按键/提示音设置（Tikey 类功能）研究报告

> 分析日期：2026-10-18
> 目标：将「按钮声音设置」（类似 [Tikey](https://apps.apple.com/app/tikey/id1504776402)）集成到 MacResourceMonitor。

---

## 1. 结论（先看结论）

**可以实现** — 但 Tikey 在 macOS 上并没有一个完整的「按键声」开关。Tikey 实际控制的是
macOS **「声音·声音效果」面板** 的三个用户可见选项：

| 功能 | 对应的 macOS preference key | 类型 |
|------|---------------------------|------|
| 提示音音量（Alert volume） | `com.apple.sound.beep.volume` | float 0.0~1.0 |
| 开启界面声音效果（Play user interface sound effects） | `com.apple.sound.beep` | bool |
| 播放声音时闪屏（Flash screen when playing sound effects） | `com.apple.sound.beep.flash` | bool |

> 键盘「按键声」本身来自输入法实现，macOS 没有暴露给第三方应用的全局开关，
> 因此不能完整复刻 Tikey 的「监听按键播放音效」这类深层功能。
> 但音量滑块 + 声音效果开关是 Tikey 主要的用户-facing 功能。

**实现路径**：方案 A — `osascript`（官方音量接口）读写提示音音量 + `defaults` 读写开关。

---

## 2. 调研过程

### 2.1 验证的接口

1. `osascript -e "get volume settings"` → 输出 `output volume:N, input volume:N, alert volume:N, output muted:B`
2. `osascript -e "set volume alert volume 70"` → 立即生效，0~100 整数
3. `defaults read -g com.apple.sound.beep.volume` → float 0.0~1.0

本地实验（当前机器）：

```
defaults float: 0.6065307  ← 系统偏好里的原始值
osascript alert volume: 50 ← 系统音量的 50%

→ set volume alert volume 70
   defaults float 变为 0.7408182
   osascript alert volume: 70

→ set volume alert volume 50
   osascript alert volume: 50
```

**发现**：`defaults` 的 float 值 (`0.6065307`) 与 `osascript` 报出的百分比 (`50`)
**不满足线性关系** —— macOS 在内部做了一个 perceptual-mapping (类似 γ/cube-root)。
这意味着：

- **`osascript` 是用户看到的「声音效果音量滑块」取值**（线性百分比），推荐作为
  唯一数据源；
- `defaults` 的 float 是内部储存，绕过 osascript 读写容易产生滑块不一致。

> 因此本方案把 `osascript` 作为主要读写路径，`defaults` 仅作 fallback。

### 2.2 权限/沙箱分析

| 操作 | 当前 Python 沙箱下是否可用 | 备注 |
|------|------------------------|------|
| `osascript -e "set volume alert volume N"` | ✅ | AppleScript 音量是用户代理设置，无额外权限 |
| `osascript -e "get volume settings"` | ✅ | 同上 |
| `defaults write -g com.apple.sound.beep.volume` | ✅ | NSGlobalDomain 偏好写，sandbox 默认允许 |
| `defaults write -g com.apple.sound.beep` | ✅ | 同上 |
| CGEventTap / Accessibility 监听 | ❌ | 需要 Input Monitoring，App Sandbox **不可用** |
| IOHID 监听键盘 | ❌ | 需要硬件 entitlement，沙箱拒绝 |

**结论**：方案 A 完全在当前 Python + rumps 架构下实现，**无任何额外权限、无沙箱冲突。**

---

## 3. 方案A实现细节

### 3.1 抽象 API（key_sound.py，已原型）

```python
import key_sound as ks
ks.status()
# {'enabled': False, 'volume': 0.5, 'flash': False}

ks.alert_volume()      # -> 0.0~1.0
ks.set_alert_volume(0.3)  # -> True (immediately effective via osascript)

ks.ui_sounds_enabled()
ks.set_ui_sounds(True)   # 开启界面声音效果

ks.flash_when_beep()
ks.set_flash_when_beep(False)
```

### 3.2 在应用菜单中的集成

在 `SystemMonitorApp` 新增「声音设置」子菜单：

```
声音设置
├── 界面声音效果      [打勾]        ← 切换 com.apple.sound.beep
├── 闪屏提示          [打勾]         ← 切换 com.apple.sound.beep.flash
├── 提示音音量 50%     (点击 → 快速选择 0/25/50/75/100%)
└── 设为全局提示音量   ←（可选）打开系统声音设置
```

菜单中「提示音音量」采用 **离散分档菜单项**（0/10/20/.../100%）
作为 MVP 的滑块替代；当前值的菜单项显示实时百分比。

> rumps.MenuItem 不支持原生 Slider，若将来需要连续滑块，
> 可以弹出一个极小的 NSPanel + NSSlider（类似 Tikey 的浮窗）。

### 3.3 配置持久化

将当前状态镜像到 `config.json`（不改变系统偏好，仅用于快速恢复）：

```json
"key_sound": {
  "alert_volume": 0.5,
  "ui_sounds": true,
  "flash": false
}
```

---

## 4. Swift vs Python+PyObjC

| 维度 | Swift | Python + PyObjC |
|------|-------|-----------------|
| osascript / defaults 操作 | Process.run | subprocess (一致) |
| 提示音音量滑块悬浮窗 | 原生 NSSlider 面板 | PyObjC NSPanel + NSStepper 或 NSTextField |
| CGEventTap（完整 Tikey 功能） | 原生更容易 | PyObjC 可用但桥接繁琐 |
| 与本项目集成 | 需额外进程/IPC | 直接 import key_sound |
| 推荐 | 只在「完整键盘监听」阶段考虑 | **当前阶段足够** |

---

## 5. 风险点 & 验证方式

| 风险 | 等级 | 验证 |
|------|------|------|
| osascript 在无声音设备时报错 | 中 | `subprocess` 带 timeout + rc 校验；失败回退 defaults |
| defaults float 非线性导致滑块回跳 | 高 | 统一使用 osascript 读写 |
| 点击切换后 UI 未及时刷新 | 低 | 菜单点击后立刻 `set_volume`+`readback` 更新标题 |
| 沙箱/签名限制 | 低 | 当前未沙箱；defaults/-g 域写在未署名场景也正常 |

本地已验证：`osascript set volume alert volume 70 / 50` 双向一致，`defaults read/write -g com.apple.sound.beep*` 可正常。

---

## 6. 下一步

1. **MVP**：`key_sound.py` 原型（已在 `MacResourceMonitorFixed/key_sound.py`）
   加入菜单「声音设置」子菜单 + 离散分档音量。
2. **如需更细体验**：弹出 `NSPanel` + `NSSlider` 实现连续滑块
   （代码量+80 行，与 FloatWindow 方案 A 技术一致）。
3. **不要**去实现 CGEventTap/IOHID——沙箱不可用、用户授权门槛高、
   收益低（macOS 原本无全局「按键声」概念）。

---

> 文档原型 `key_sound.py` 已随报告提交至仓库。
EOF
echo "---written---"