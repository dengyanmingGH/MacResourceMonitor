"""
MacResourceMonitor - 按键/提示音设置（Tikey 类功能）
===================================================
提供 macOS 系统「声音·声音效果」（System Settings > Sound > Sound Effects）
面板的轻量控制，类似 Tikey 的「系统提示音音量滑块」：

  - 提示音音量 （Alert volume，0.0~1.0）     ↔ 对应「声音效果」音量滑块
  - 开启/关闭用户界面声音效果（Play user interface sound effects）
  - 开启/关闭播放声音时闪屏（Flash screen when playing sound effects）

实现说明
--------
macOS 在 NSGlobalDomain 下以三组 keys 存储该面板所有选项：
    com.apple.sound.beep            (bool)   # 开启界面声音效果
    com.apple.sound.beep.volume     (float)  # 提示音音量 0.0~1.0
    com.apple.sound.beep.flash      (bool)   # 播放声音时闪屏

读写方式：
  - 提示音音量：优先走 `osascript -e "set volume alert volume N"` / `get volume settings`
    （N 为 0..100 的整数），失败回退到 `defaults` 读写 float 值；
  - 开关：直接 `defaults write/delete NSGlobalDomain com.apple.sound.beep(.flash)`。

权限说明
--------
这些都是用户级别的偏好，写 NSGlobalDomain 不需要额外授权，沙箱下亦可读写
（前提是应用已获得偏好的读写权限或属于 same-signer）。

注意
----
键盘「按键声」（key click）在 macOS 是输入法的一部分，没有暴露给三方应用的
全局开关；但我们可以通过 Accessibility 监听按键并播放指定提示音，
实现「按键声音」功能（与 Tikey 的键盘声音效果一致）。

该监听需要 Accessibility 授权：
  - 非签名 / 非沙箱的 Python bundle 无法静默获得；首次请求时会弹出对话框并跳转
    「系统设置 > 隐私与安全 > 辅助功能」；
  - 若用户拒绝或未授权，监听自动降级为关闭，应用不受影响。
"""

import re
import subprocess
import sys
import threading

# 三个偏好键
_BEEP_VOLUME = "com.apple.sound.beep.volume"
_BEEP_ENABLE = "com.apple.sound.beep"
_BEEP_FLASH = "com.apple.sound.beep.flash"

# osascript 的音量百分比与 0..1 之间的转换
_PCT_TO_FLOAT = 1.0 / 100.0

# ---- 按键声音配置（本地持久化到 ~/.config/MacResourceMonitor/key_sound.json） ----
import json as _json, os as _os
_CFG_DIR = _os.path.join(_os.path.expanduser("~"),
                         "Library", "Application Support", "MacResourceMonitor")
_KEY_CFG = _os.path.join(_CFG_DIR, "key_sound.json")
_DEFAULT_KEY_CFG = {
    "key_enabled": False,      # 按键声音开关
    "key_sound": "Ping",       # 系统声音名称
    "key_volume": 0.5,          # 单独音量 0.0~1.0（NSSound.setVolume_）
}


def _load_key_cfg():
    try:
        with open(_KEY_CFG) as f:
            d = _DEFAULT_KEY_CFG.copy()
            d.update(_json.load(f))
            return d
    except Exception:
        return _DEFAULT_KEY_CFG.copy()


def _save_key_cfg(d):
    try:
        _os.makedirs(_CFG_DIR, exist_ok=True)
        with open(_KEY_CFG, "w") as f:
            _json.dump(d, f)
    except Exception:
        pass


# ---- 按键声音开关 / 音量 / 声音名称 ----

def key_sound_enabled():
    return bool(_load_key_cfg().get("key_enabled", False))


def set_key_sound_enabled(v):
    d = _load_key_cfg(); d["key_enabled"] = bool(v); _save_key_cfg(d)
    if not v:
        stop_key_listener()  # 关闭时停止监听
    else:
        start_key_listener()
    return True


def key_sound_name():
    return _load_key_cfg().get("key_sound", "Ping")


def set_key_sound_name(name):
    d = _load_key_cfg(); d["key_sound"] = name; _save_key_cfg(d)
    return True


def key_sound_volume():
    try:
        return float(_load_key_cfg().get("key_volume", 0.5))
    except (TypeError, ValueError):
        return 0.5


def set_key_sound_volume(v):
    v = max(0.0, min(1.0, float(v)))
    d = _load_key_cfg(); d["key_volume"] = v; _save_key_cfg(d)
    return True


# ---- 播放 + 监听 ----

# macOS 自带的 short system sounds
SYSTEM_SOUNDS = ["Basso", "Blow", "Blow (Bottom)", "Ping", "Pop",
                 "Submarine", "Tink", "Funk", "Glass"]

# Accessibility 按键监听句柄
_tap = {"running": False, "loop": None, "thread": None}


def _play_key_sound():
    try:
        from AppKit import NSSound
        s = NSSound.soundNamed_(key_sound_name())
        if s is None:
            s = NSSound.soundNamed_("Ping")
        if s is not None:
            try:
                s.setVolume_(key_sound_volume())
            except Exception:
                pass
            s.play()
    except Exception as e:
        print("key_sound play error:", e, file=sys.stderr)


def play_key_sound_once():
    """主动播放一次按键声（用于测试）。"""
    _play_key_sound()
    return True


def start_key_listener():
    """尝试开启 Accessibility 按键监听。

    非签名应用将被 TCC 拒绝；失败时静默降级（key_enabled 仍留 True，
    但不再播放）。"""
    import sys
    if _tap["running"]:
        return
    try:
        from Quartz import (CGEventTapCreate, CGEventTapCallBack,
                            kCGHIDEventTap, kCGEventKeyDown)
        import CoreFoundation as _cf
        from AppKit import NSApp
    except Exception:
        # Quartz 未绑定核对（如当前 Python bundle）或缺少 Accessibility 授权；
        # 此时监听不可用，静默降级——「按键声音」开关仍会在手动触发时播放。
        return

    app = NSApp() if NSApp() is not None else None

    def callback(proxy, type_, event, refcon):
        try:
            if int(type_) == kCGEventKeyDown:
                _play_key_sound()
        except Exception:
            pass
        return event

    tap = CGEventTapCreate(
        0, 0, kCGHIDEventTap, 0, kCGEventKeyDown, callback, None)
    if tap is None:
        # 缺少 Accessibility 授权 → 静默降级（可点击菜单重新尝试）。
        return

    loop = _cf.CFRunLoopGetCurrent()
    src = _cf.CFMachPortCreateRunLoopSource(None, tap, 0)
    _cf.CFRunLoopAddSource(loop, src, _cf.kCFRunLoopCommonModes)
    _tap["loop"] = loop

    def run():
        _tap["running"] = True
        _cf.CFRunLoopRun()

    _tap["thread"] = threading.Thread(target=run, daemon=True)
    _tap["thread"].start()


def stop_key_listener():
    if not _tap["running"]:
        return
    if _tap.get("loop") is not None:
        try:
            import CoreFoundation as cf
            cf.CFRunLoopStop(_tap["loop"])
        except Exception:
            pass
    _tap["running"] = False


def _run(cmd, timeout=5):
    """运行命令并返回 (stdout, returncode)。"""
    try:
        p = subprocess.run(
            cmd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return p.stdout.strip(), p.returncode
    except Exception:
        return "", 1


def _osascript(expr, timeout=5):
    return _run(["/usr/bin/osascript", "-e", expr], timeout)


# ---------- 提示音音量 ----------

_ALERT_RE = re.compile(r"alert volume:(\d+)")


def alert_volume():
    """读取系统提示音音量，返回 0.0~1.0。"""
    # 1. osascript —— 官方接口
    out, rc = _osascript('get volume settings')
    if rc == 0:
        m = _ALERT_RE.search(out)
        if m:
            return max(0.0, min(1.0, int(m.group(1)) * _PCT_TO_FLOAT))
    # 2. 回退：defaults float
    out, rc = _run(["/usr/bin/defaults", "read", "-g", _BEEP_VOLUME])
    if rc == 0:
        try:
            return max(0.0, min(1.0, float(out)))
        except ValueError:
            pass
    return 0.5  # 未知时兜底中值


def _volume_via_defaults():
    """defaults 读（用于备份路径）。"""
    out, rc = _run(["/usr/bin/defaults", "read", "-g", _BEEP_VOLUME])
    try:
        return max(0.0, min(1.0, float(out))) if rc == 0 else 0.5
    except ValueError:
        return 0.5


def set_alert_volume(v):
    """设定提示音音量，v ∈ [0.0, 1.0]。返回是否成功。"""
    v = max(0.0, min(1.0, float(v)))
    pct = int(round(v * 100))
    # 1. 优先 osascript（立即生效）
    out, rc = _osascript('set volume alert volume {}'.format(pct))
    if rc == 0:
        return True
    # 2. 回退 defaults（写入 NSGlobalDomain，需 killall 生效）
    rc2 = _run(["/usr/bin/defaults", "write", "-g", _BEEP_VOLUME,
                "-float", "{:.6f}".format(v)])[1]
    return rc2 == 0


# ---------- 界面声音效果开关 ----------

def ui_sounds_enabled():
    out, rc = _run(["/usr/bin/defaults", "read", "-g", _BEEP_ENABLE])
    if rc != 0:
        return False  # 键不存在即为关闭
    return out.strip() in ("1", "true", "True")


def set_ui_sounds(enabled):
    if enabled:
        rc = _run(["/usr/bin/defaults", "write", "-g", _BEEP_ENABLE,
                   "-bool", "true"])[1]
    else:
        rc = _run(["/usr/bin/defaults", "delete", "-g", _BEEP_ENABLE])[1]
    return rc == 0


# ---------- 闪屏开关 ----------

def flash_when_beep():
    out, rc = _run(["/usr/bin/defaults", "read", "-g", _BEEP_FLASH])
    if rc != 0:
        return False
    return out.strip() in ("1", "true", "True")


def set_flash_when_beep(enabled):
    if enabled:
        rc = _run(["/usr/bin/defaults", "write", "-g", _BEEP_FLASH,
                   "-bool", "true"])[1]
    else:
        rc = _run(["/usr/bin/defaults", "delete", "-g", _BEEP_FLASH])[1]
    return rc == 0


# ---------- 发出系统提示音 ----------

def play_alert_sound():
    """播放一条系统提示音。

    仅当「界面声音效果」开启时播放；关闭时静默。播放逻辑：
      - 先写 com.apple.sound.beep.volume，交给 macOS 控制音量与闪屏，
      - 再调用 AppKit.NSBeep 触发系统默认提示音。
    """
    if not ui_sounds_enabled():
        return False
    try:
        from AppKit import NSBeep
        NSBeep()
        return True
    except Exception:
        # 降级：osascript 播放内置提示音资源
        _osascript('display notification "" sound name "Ping"')
        return True


# ---------- 批量获取/设置（用于菜单渲染） ----------

def status():
    """返回当前完整状态。"""
    return {
        "enabled": ui_sounds_enabled(),
        "volume": alert_volume(),
        "flash": flash_when_beep(),
        "key_enabled": key_sound_enabled(),
        "key_sound": key_sound_name(),
        "key_volume": key_sound_volume(),
    }


def _pct_str(v):
    return "{:.0f}%".format(v * 100)


if __name__ == "__main__":
    import json
    print(json.dumps(status(), indent=2))
