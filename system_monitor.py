"""
macOS System Resource Monitor - Menu Bar App
=============================================
数据来源:
  - CPU / 内存 / 磁盘 / 网络 / Swap : psutil            (无需权限)
  - 温度 (CPU/GPU/电池) / 风扇上限    : iSMC all         (无需权限)
  - 风扇当前转速                     : iSMC raw         (无需权限，低频)
  - GPU 使用率/频率/功率             : powermetrics     (免密 sudo)
  - CPU 频率 / 热压力                : powermetrics     (免密 sudo)

配置持久化:
  ~/Library/Application Support/MacResourceMonitor/config.json
  可在菜单中实时修改（菜单栏显示指标 / powermetrics 采样间隔 /
  温度告警开关与阈值），无需改代码、无需重新构建。

snapshot 结构（Collector.get() 返回）:
  ts, cpu, cpu_cores, cpu_per_core[], mem_*, swap_*, disk_*,
  net_*, net_up_mb, net_down_mb, dio_read_mb, dio_write_mb,
  battery_*, temps{}, fan_current, fan_max, pm{}, pm_ts,
  procs[], hist{}, peaks{}, health{}
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

import psutil
import rumps
import objc
from Foundation import NSObject, NSAttributedString
from AppKit import (NSView, NSBezierPath, NSColor, NSFont, NSTextField,
                    NSMakeRect, NSMakePoint, NSMakeSize)

# ============================================================
# 配置持久化
# ============================================================

APP_NAME = "MacResourceMonitor"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "title_metric": "mem",     # mem/cpu/disk/gpu/temp/fan/net/battery
    "pm_interval": 8.0,        # powermetrics 采样间隔(秒)，0=关闭
    "fan_interval": 10.0,      # 风扇转速采样间隔(秒)
    "temp_threshold": 90.0,    # CPU 温度告警阈值(°C)
    "alerts_enabled": True,    # 温度告警总开关
    "thermal_alert": True,     # 热压力告警开关
    "history_len": 60,         # 详情窗口曲线保留点数
}


def load_config():
    """读取配置，缺失/损坏字段回退默认值。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, dv in DEFAULT_CONFIG.items():
                v = data.get(k)
                if type(v) == type(dv):  # noqa: E721 严格类型匹配，避免 bool/int 混淆
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg):
    """原子写入配置（临时文件 + rename），避免写一半损坏。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        print("save config error:", e, file=sys.stderr)


# ============================================================
# 工具路径探测
# ============================================================

def find_ismc():
    """按优先级查找 iSMC：环境变量 > Homebrew > MacPorts/本地 > PATH"""
    for c in (os.environ.get("MRM_ISMC"),
              "/opt/homebrew/bin/iSMC",
              "/usr/local/bin/iSMC",
              shutil.which("iSMC")):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "iSMC"


ISMC = find_ismc()
ISMC_EXISTS = os.path.isfile(ISMC) and os.access(ISMC, os.X_OK)
POWERMETRICS = "/usr/bin/powermetrics"


def _run_rc(cmd, timeout=6):
    """执行命令，返回 (stdout, returncode)；异常时返回 ("", -1)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or ""), r.returncode
    except Exception:
        return "", -1


def _run(cmd, timeout=6):
    return _run_rc(cmd, timeout)[0]


# ============================================================
# 数据采集
# ============================================================

# 已在 M1 MacBookPro17,1 上用 macmon 交叉验证：
#  * "CPU Die Average/Max" (TCMb/TCMz) 是真实的核心温度传感器。
#  * M1 的 GPU 核心温度传感器（Tg1b/Tg4b）读数为 0，即不可用；
#    macmon 对此机型也返回 gpu_temp_avg = None。
#    因此读取的 "GPU Heatsink 1" (TG0H) 是散热器/板级探针温度，
#    不是 GPU 核心温度，展示时必须明确标注，避免与 CPU 核心温度混淆。
TEMP_MAP = {
    "cpu_avg": "CPU Die Average",
    "cpu_max": "CPU Die Max",
    "gpu_heatsink": "GPU Heatsink 1",   # 散热器温度，非 GPU 核心
    "heatpipe": "CPU Heatpipe",
    "battery": "Battery 1",
    "ssd": "NAND",
    "airport": "Airport 1",
}


def collect_temperatures():
    """iSMC all -o json 一次调用拿全部温度 + 风扇上限（无需 sudo）。"""
    out = _run([ISMC, "all", "-o", "json"])
    if not out:
        return {}
    try:
        data = json.loads(out)
    except Exception:
        return {}
    temps_raw = data.get("Temperature") or {}
    fans_raw = data.get("Fans") or {}
    result = {}
    for short, key in TEMP_MAP.items():
        item = temps_raw.get(key)
        if isinstance(item, dict) and isinstance(item.get("quantity"), (int, float)):
            result[short] = float(item["quantity"])
    item = fans_raw.get("Fan 1 Maximum Speed")
    if isinstance(item, dict) and isinstance(item.get("quantity"), (int, float)):
        result["fan_max"] = float(item["quantity"])
    return result


def collect_fan_raw():
    """通过 iSMC raw 读取风扇当前转速（F0Ac）与上限（F0Mx）。"""
    out = _run([ISMC, "raw"])
    if not out:
        return None, None
    current = None
    maximum = None
    for line in out.split("\n"):
        # 形如: "  F0Ac  [flt ]  0 (bytes 00 00 00 00)"
        m = re.match(r"\s*F0Ac\s+\[[^\]]*\]\s+([\d.]+)", line)
        if m:
            current = float(m.group(1))
        m2 = re.match(r"\s*F0Mx\s+\[[^\]]*\]\s+([\d.]+)", line)
        if m2:
            maximum = float(m2.group(1))
    return current, maximum


def collect_gpu_and_cpu():
    """通过 powermetrics（免密 sudo）读取 GPU 使用率、频率、功率、CPU 频率、热压力。"""
    result = {}
    out = _run(["sudo", "-n", POWERMETRICS, "--samplers",
                "gpu_power,cpu_power,thermal", "-n", "1", "-i", "300"], timeout=8)
    if not out:
        return result

    for line in out.split("\n"):
        line = line.strip()

        # GPU 使用率 = active residency
        if line.startswith("GPU HW active residency:"):
            m = re.search(r"([\d.]+)%", line)
            if m:
                result["gpu_usage"] = float(m.group(1))
        # GPU 频率
        elif line.startswith("GPU HW active frequency:"):
            m = re.search(r"([\d.]+)\s*MHz", line)
            if m:
                result["gpu_freq"] = float(m.group(1))
        # GPU 功率
        elif line.startswith("GPU Power:"):
            m = re.search(r"([\d.]+)\s*mW", line)
            if m:
                result["gpu_power"] = float(m.group(1))
        # CPU 功率
        elif line.startswith("CPU Power:"):
            m = re.search(r"([\d.]+)\s*mW", line)
            if m:
                result["cpu_power"] = float(m.group(1))
        # P 核频率
        elif line.startswith("P-Cluster HW active frequency:"):
            m = re.search(r"([\d.]+)\s*MHz", line)
            if m:
                result["p_freq"] = float(m.group(1))
        # E 核频率
        elif line.startswith("E-Cluster HW active frequency:"):
            m = re.search(r"([\d.]+)\s*MHz", line)
            if m:
                result["e_freq"] = float(m.group(1))
        # 热压力
        elif line.startswith("Current pressure level:"):
            result["thermal"] = line.split(":", 1)[1].strip()
    return result


def collect_disk():
    """APFS 容器级磁盘使用率。

    注意：macOS 上 '/' 是只读的系统卷（只含 ~12GB 系统文件），
    用户数据在 '/System/Volumes/Data'。若直接对 '/' 求使用率会得到
    严重偏低的假数据（例如实际 71% 会显示成 15%）。

    这两个卷共享同一个 APFS 容器，因此对数据卷调用 statvfs 时，
    f_blocks 返回的是整个容器大小，f_bavail 是容器可用空间，
    二者相减即为整个磁盘的真实已用量。
    """
    path = "/System/Volumes/Data"
    if not os.path.exists(path):
        path = "/"

    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    percent = (used / total * 100.0) if total else 0.0

    return {
        "percent": percent,
        "used_gb": used // (1024 ** 3),
        "total_gb": total // (1024 ** 3),
        "free_gb": free // (1024 ** 3),
    }


def collect_basic():
    """psutil 基础指标（无需权限）。"""
    m = {}
    try:
        m["cpu"] = psutil.cpu_percent(interval=None)
    except Exception:
        m["cpu"] = 0.0
    try:
        cores = psutil.cpu_percent(interval=None, percpu=True)
        m["cpu_per_core"] = [float(c) for c in cores]
        m["cpu_cores"] = len(cores)
    except Exception:
        m["cpu_per_core"] = []
        m["cpu_cores"] = psutil.cpu_count() or 1
    try:
        mem = psutil.virtual_memory()
        m["mem_percent"] = mem.percent
        m["mem_used_mb"] = mem.used // (1024 * 1024)
        m["mem_total_mb"] = mem.total // (1024 * 1024)
        # macOS 内存压力参考：活跃 + 联动 / wired
        m["mem_active_mb"] = getattr(mem, "active", 0) // (1024 * 1024)
        m["mem_wired_mb"] = getattr(mem, "wired", 0) // (1024 * 1024)
    except Exception:
        pass
    try:
        sw = psutil.swap_memory()
        m["swap_used_mb"] = sw.used // (1024 * 1024)
        m["swap_total_mb"] = sw.total // (1024 * 1024)
    except Exception:
        pass
    try:
        d = collect_disk()
        m["disk_percent"] = d["percent"]
        m["disk_used_gb"] = d["used_gb"]
        m["disk_total_gb"] = d["total_gb"]
        m["disk_free_gb"] = d["free_gb"]
    except Exception:
        pass
    try:
        n = psutil.net_io_counters()
        m["net_sent"] = n.bytes_sent
        m["net_recv"] = n.bytes_recv
    except Exception:
        pass
    try:
        dio = psutil.disk_io_counters()
        m["dio_read"] = dio.read_bytes
        m["dio_write"] = dio.write_bytes
    except Exception:
        pass
    # 电池
    try:
        b = psutil.sensors_battery()
        if b:
            m["battery_percent"] = b.percent
            m["battery_plugged"] = b.power_plugged
    except Exception:
        pass
    return m


def sample_processes(prev):
    """采样进程 CPU/内存，prev 为 pid -> 上次 cpu_percent 的字典（原地更新）。

    cpu_percent 是增量值：第一次看到的进程没有基线，CPU 记 0（否则全是假值），
    但内存是绝对值，因此新进程仍会参与内存排行。
    返回 (按 CPU 降序前 5, 按内存降序前 5)，元素为 [(name, cpu, rss_mb), ...]
    """
    out = []
    alive = set()
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = p.info
            pid = info["pid"]
            alive.add(pid)
            cpu = info.get("cpu_percent") or 0.0
            mi = info.get("memory_info")
            rss_mb = (mi.rss // (1024 * 1024)) if mi else 0
            if pid not in prev:
                prev[pid] = cpu
                cpu = 0.0        # 无增量基线
            else:
                prev[pid] = cpu
            name = (info.get("name") or "?").split("/")[-1]
            out.append((name, cpu, rss_mb))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    for pid in list(prev.keys()):
        if pid not in alive:
            del prev[pid]
    by_cpu = sorted(out, key=lambda x: x[1], reverse=True)[:5]
    by_mem = sorted(out, key=lambda x: x[2], reverse=True)[:5]
    return by_cpu, by_mem


# ============================================================
# 后台采集线程（多速率调度）
# ============================================================

class Collector(threading.Thread):
    """每 interval 秒采集基础指标；温度/风扇/powermetrics 各自按独立间隔。

    - 温度   : 每 interval      (iSMC all，1 次子进程)
    - 风扇   : 每 fan_interval  (iSMC raw，缓存)
    - GPU/CPU: 每 pm_interval   (powermetrics，缓存；0=关闭)

    iSMC 连续失败后自动退避 30s，避免无谓地反复 spawn 报错。
    """

    def __init__(self, interval=2.0, config=None):
        super().__init__(daemon=True)
        self.interval = interval
        self.config = config if config is not None else load_config()
        self.snapshot = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()

        self._net_prev = None
        self._net_rate = (0.0, 0.0)
        self._dio_prev = None
        self._dio_rate = (0.0, 0.0)

        self._pm_cache = {}
        self._pm_ts = 0.0
        self._pm_next = 0.0
        self._pm_fail = 0
        self._pm_note = ""

        self._temps_cache = {}
        self._fan_cache = (None, None)
        self._fan_max_cached = None
        self._fan_next = 0.0

        self._ismc_fail = 0
        self._ismc_backoff_until = 0.0

        hlen = max(10, int(self.config.get("history_len", 60)))
        self.hist = {
            "cpu": deque(maxlen=hlen),
            "mem": deque(maxlen=hlen),
            "gpu": deque(maxlen=hlen),
            "temp": deque(maxlen=hlen),
        }
        self._peaks = {"cpu": 0.0, "mem": 0.0, "gpu": 0.0, "temp": 0.0}

        self._proc_prev = {}
        self.track_processes = False
        self._procs_cache = ([], [])     # (按 CPU 前 5, 按内存前 5)

    def get(self):
        with self.lock:
            return dict(self.snapshot)

    def stop(self):
        self._stop.set()

    def set_track_processes(self, on):
        """仅在详情窗口可见时采集进程排行，隐藏时零开销。"""
        if on and not self.track_processes:
            # 建立基线，下一轮起才有增量数据
            try:
                sample_processes(self._proc_prev)
            except Exception:
                pass
        self.track_processes = bool(on)

    def force_pm(self):
        """命令下一次循环立即采集 powermetrics。"""
        self._pm_next = 0.0

    def _pm_reason(self):
        """powermetrics 无输出时诊断原因（只在失败时调用）。"""
        _, rc = _run_rc(["sudo", "-n", "true"])
        if rc != 0:
            return "sudo 免密未配置"
        return "powermetrics 无输出"

    def run(self):
        # 预热 cpu_percent（全局与 per-core 各自缓存），避免首次读到 0
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        time.sleep(1.0)
        while not self._stop.is_set():
            t0 = time.time()
            now = t0
            snap = {}

            basic = collect_basic()

            # 网络速率（MB/s）
            if self._net_prev and "net_sent" in basic:
                dt = max(t0 - self._net_prev[0], 0.001)
                up = (basic["net_sent"] - self._net_prev[1]) / dt / (1024 * 1024)
                down = (basic["net_recv"] - self._net_prev[2]) / dt / (1024 * 1024)
                self._net_rate = (max(up, 0), max(down, 0))
            if "net_sent" in basic:
                self._net_prev = (t0, basic["net_sent"], basic["net_recv"])
            basic["net_up_mb"] = self._net_rate[0]
            basic["net_down_mb"] = self._net_rate[1]

            # 磁盘 I/O 速率（MB/s）
            if self._dio_prev and "dio_read" in basic:
                dt = max(t0 - self._dio_prev[0], 0.001)
                r = (basic["dio_read"] - self._dio_prev[1]) / dt / (1024 * 1024)
                w = (basic["dio_write"] - self._dio_prev[2]) / dt / (1024 * 1024)
                self._dio_rate = (max(r, 0), max(w, 0))
            if "dio_read" in basic:
                self._dio_prev = (t0, basic["dio_read"], basic["dio_write"])
            basic["dio_read_mb"] = self._dio_rate[0]
            basic["dio_write_mb"] = self._dio_rate[1]

            snap.update(basic)

            # ---- 温度（iSMC all，含退避） ----
            temps_ok = False
            if now >= self._ismc_backoff_until:
                temps = collect_temperatures()
                if temps:
                    temps_ok = True
                    self._ismc_fail = 0
                    self._temps_cache = temps
                    if "fan_max" in temps:
                        self._fan_max_cached = temps["fan_max"]
                else:
                    self._ismc_fail += 1
                    if self._ismc_fail >= 3:
                        # 连续失败：退避 30s，界面显示缓存值 + 异常状态
                        self._ismc_backoff_until = now + 30.0
                        self._ismc_fail = 0
            snap["temps"] = dict(self._temps_cache)
            snap["temps_ok"] = temps_ok or bool(self._ismc_backoff_until > now)

            # ---- 风扇当前转速（iSMC raw，低频） ----
            fan_interval = float(self.config.get("fan_interval", 10.0) or 0)
            if fan_interval > 0 and now >= self._fan_next:
                cur, mx = collect_fan_raw()
                if cur is not None or mx is not None:
                    self._fan_cache = (cur, mx)
                    if mx is not None:
                        self._fan_max_cached = mx
                self._fan_next = now + fan_interval
            snap["fan_current"] = self._fan_cache[0]
            snap["fan_max"] = self._fan_max_cached

            # ---- GPU / CPU 频率 / 热压力（powermetrics，独立间隔） ----
            pm_interval = float(self.config.get("pm_interval", 8.0) or 0)
            pm_fresh = False
            if pm_interval > 0 and now >= self._pm_next:
                pm = collect_gpu_and_cpu()
                if pm:
                    self._pm_cache = pm
                    self._pm_ts = now
                    self._pm_fail = 0
                    pm_fresh = True
                else:
                    self._pm_fail += 1
                    if self._pm_note == "" or self._pm_fail % 3 == 1:
                        self._pm_note = self._pm_reason()
                self._pm_next = now + pm_interval
            snap["pm"] = dict(self._pm_cache)
            snap["pm_ts"] = self._pm_ts
            snap["pm_fresh"] = pm_fresh
            snap["pm_enabled"] = pm_interval > 0

            # ---- 进程排行（仅详情窗口可见时） ----
            if self.track_processes:
                try:
                    self._procs_cache = sample_processes(self._proc_prev)
                except Exception:
                    pass
            top_cpu, top_mem = self._procs_cache
            snap["procs"] = list(top_cpu)
            snap["procs_mem"] = list(top_mem)

            # ---- 历史曲线 ----
            h = self.hist
            h["cpu"].append(basic.get("cpu"))
            h["mem"].append(basic.get("mem_percent"))
            gpu_v = snap["pm"].get("gpu_usage")
            if gpu_v is None and h["gpu"]:
                gpu_v = h["gpu"][-1]  # powermetrics 未刷新时保持上一值
            h["gpu"].append(gpu_v)
            t_v = snap["temps"].get("cpu_avg")
            if t_v is None and h["temp"]:
                t_v = h["temp"][-1]
            h["temp"].append(t_v)
            snap["hist"] = {k: list(v) for k, v in h.items()}

            # ---- 会话峰值 ----
            for k, v in (("cpu", basic.get("cpu")), ("mem", basic.get("mem_percent")),
                         ("gpu", snap["pm"].get("gpu_usage")),
                         ("temp", snap["temps"].get("cpu_avg"))):
                if isinstance(v, (int, float)) and v > self._peaks[k]:
                    self._peaks[k] = v
            snap["peaks"] = dict(self._peaks)

            # ---- 数据源健康状态 ----
            if not ISMC_EXISTS:
                ismc_state = "未安装"
            elif now < self._ismc_backoff_until:
                ismc_state = "异常"
            else:
                ismc_state = "正常"
            if pm_interval <= 0:
                pm_state = "已关闭"
            elif snap["pm"]:
                age = now - self._pm_ts
                pm_state = "正常" if age < pm_interval * 2.5 else "{:.0f}秒前".format(age)
            else:
                pm_state = self._pm_note or "无数据"
            snap["health"] = {"ismc": ismc_state, "pm": pm_state}

            snap["ts"] = now
            with self.lock:
                self.snapshot = snap

            elapsed = time.time() - t0
            self._stop.wait(max(self.interval - elapsed, 0.5))


# ============================================================
# 详情窗口（macOS 风格：卡片 / 分段控件 / 大图 / 数据行）
# ============================================================

from AppKit import (NSGradient, NSSegmentedControl, NSTextAlignment,
                    NSTextAlignmentRight, NSTextAlignmentLeft,
                    NSTextAlignmentCenter, NSWindowStyleMaskTitled,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskFullSizeContentView,
                    NSMakeRect)

# 曲线可切换指标: (hist key, 分段标签, 单位)
GRAPH_METRICS = [
    ("cpu", "CPU", "%"),
    ("mem", "内存", "%"),
    ("gpu", "GPU", "%"),
    ("temp", "温度", "°C"),
]

GRAPH_COLORS = {
    "cpu": "systemBlueColor",
    "mem": "systemGreenColor",
    "gpu": "systemPurpleColor",
    "temp": "systemOrangeColor",
}

# 曲线指标 -> 进程排行维度（随分段控件联动）
#   (卡片标题, snapshot 键, 排序依据)
# GPU / 温度没有逐进程数据源，按需求回退：GPU 看 CPU 排行，温度看内存排行。
PROC_RANKING = {
    "cpu":  ("CPU 占用 Top 5",  "procs",     "cpu"),
    "mem":  ("内存占用 Top 5", "procs_mem", "mem"),
    "gpu":  ("CPU 占用 Top 5",  "procs",     "cpu"),
    "temp": ("内存占用 Top 5", "procs_mem", "mem"),
}

ROW_TITLES = {
    "cpu": "CPU", "freq": "CPU 频率", "mem": "内存", "swap": "Swap",
    "disk": "磁盘", "diskio": "磁盘 I/O", "net": "网络",
    "battery": "电池", "gpu": "GPU 使用率", "gpu2": "GPU 频率/功率",
    "t_cpu": "CPU 核心", "t_pipe": "CPU 热管", "t_gpu": "GPU 散热器",
    "t_ssd": "SSD", "t_bat": "电池", "t_airport": "无线网卡",
    "fan": "风扇", "thermal": "热压力",
}

CARD_RADIUS = 10.0
PROC_BAR_W = 32            # 进程行占比条最大宽度


def _sys_color(name, fallback="systemGrayColor"):
    fn = getattr(NSColor, name, None) or getattr(NSColor, fallback)
    return fn()


def _labeled_font(size=12, bold=False, mono=False):
    if mono:
        # 等宽数字字体，数字跳动时宽度不抖
        return NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0)
    if bold:
        return NSFont.boldSystemFontOfSize_(size)
    return NSFont.systemFontOfSize_(size)


def _text_width(s, font):
    """实测字符串渲染宽度（比按字符数估算准确，避免提前截断）。"""
    try:
        a = NSAttributedString.alloc().initWithString_attributes_(s, {"NSFont": font})
        return a.size().width
    except Exception:
        return 0.0


def _truncate(s, font, max_w):
    """按实际像素宽度截断，末端补省略号，保证不超出控件。"""
    if not s or max_w <= 0:
        return s or ""
    if _text_width(s, font) <= max_w:
        return s
    ell = "\u2026"
    out = s
    while out and _text_width(out + ell, font) > max_w:
        out = out[:-1]
    return out + ell if out else ""


def _fmt_proc_value(v, mode):
    """进程排行数值列：CPU 用百分比，内存用紧凑的 M/G 记法（列宽约 42pt）。"""
    if mode == "mem":
        return "{:.1f}G".format(v / 1024.0) if v >= 1024 else "{:.0f}M".format(v)
    return "{:.1f}%".format(v or 0.0)


class CardView(NSView):
    """圆角卡片容器，接近 macOS 设置页 GroupBox 的观感。"""

    def initWithFrame_(self, frame):
        self = objc.super(CardView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setCornerRadius_(CARD_RADIUS)
        layer.setBackgroundColor_(NSColor.controlBackgroundColor().CGColor())
        layer.setBorderWidth_(0.5)
        layer.setBorderColor_(NSColor.separatorColor().CGColor())
        return self


class ChartView(NSView):
    """自绘曲线：网格线、渐变面积、圆角描边、当前值圆点、告警阈值虚线。"""

    def initWithFrame_(self, frame):
        self = objc.super(ChartView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.data = []
        self.line_color = NSColor.systemBlueColor()
        self.threshold = None
        return self

    def isFlipped(self):
        return True

    def setData_color_threshold_(self, data, color, threshold=None):
        self.data = [v if isinstance(v, (int, float)) else None for v in (data or [])]
        if color is not None:
            self.line_color = color
        self.threshold = threshold if isinstance(threshold, (int, float)) else None
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        try:
            vals = [v for v in self.data if v is not None]
            if len(vals) < 2:
                return
            lo, hi = min(vals), max(vals)
            if isinstance(self.threshold, (int, float)):
                hi = max(hi, self.threshold * 1.05)
                lo = min(lo, 0.0)
            if hi - lo < 1e-9:
                lo, hi = lo - 1.0, hi + 1.0
            pad = (hi - lo) * 0.12
            lo -= pad
            hi += pad

            b = self.bounds()
            W, H = b.size.width, b.size.height
            if W <= 0 or H <= 0:
                return
            n = len(self.data)
            step = W / max(n - 1, 1)

            def y_of(v):
                y = (hi - v) / (hi - lo) * H
                return max(2.0, min(H - 2.0, y))

            # 背景网格（4 条等距细线）
            grid = NSBezierPath.bezierPath()
            grid.setLineWidth_(0.5)
            NSColor.separatorColor().colorWithAlphaComponent_(0.7).setStroke()
            for i in range(1, 4):
                gy = H * i / 4.0
                grid.moveToPoint_(NSMakePoint(0, gy))
                grid.lineToPoint_(NSMakePoint(W, gy))
            grid.stroke()

            # 告警阈值虚线（手动绘制短线段，避开 C 数组传参）
            if isinstance(self.threshold, (int, float)) and self.threshold <= hi:
                ty = y_of(self.threshold)
                dash = NSBezierPath.bezierPath()
                dash.setLineWidth_(1.2)
                NSColor.systemRedColor().colorWithAlphaComponent_(0.75).setStroke()
                x = 0.0
                while x < W:
                    dash.moveToPoint_(NSMakePoint(x, ty))
                    end_x = min(x + 4.0, W)
                    dash.lineToPoint_(NSMakePoint(end_x, ty))
                    x += 7.0
                dash.stroke()
            # 连续段（None 断点拆分）
            segments = []
            cur = []
            for i, v in enumerate(self.data):
                if v is None:
                    if len(cur) >= 2:
                        segments.append(cur)
                    cur = []
                else:
                    cur.append((i * step, y_of(v)))
            if len(cur) >= 2:
                segments.append(cur)
            if not segments:
                return
            longest = max(segments, key=len)

            # 渐变面积填充
            area = NSBezierPath.bezierPath()
            area.moveToPoint_(NSMakePoint(longest[0][0], H))
            for pt in longest:
                area.lineToPoint_(NSMakePoint(pt[0], pt[1]))
            area.lineToPoint_(NSMakePoint(longest[-1][0], H))
            area.closePath()
            try:
                grad = NSGradient.alloc().initWithStartingColor_endingColor_(
                    self.line_color.colorWithAlphaComponent_(0.30),
                    self.line_color.colorWithAlphaComponent_(0.0))
                grad.drawInBezierPath_angle_(area, 90.0)
            except Exception:
                self.line_color.colorWithAlphaComponent_(0.15).setFill()
                area.fill()

            # 描边
            line = NSBezierPath.bezierPath()
            line.setLineWidth_(1.8)
            line.setLineCapStyle_(1)
            line.setLineJoinStyle_(1)
            for seg in segments:
                line.moveToPoint_(NSMakePoint(seg[0][0], seg[0][1]))
                for pt in seg[1:]:
                    line.lineToPoint_(NSMakePoint(pt[0], pt[1]))
            self.line_color.setStroke()
            line.stroke()

            # 当前值圆点（白芯 + 彩色外圈）
            lx, ly = longest[-1]
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(lx - 4.0, ly - 4.0, 8, 8))
            NSColor.windowBackgroundColor().setFill()
            ring.fill()
            dot = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(lx - 2.6, ly - 2.6, 5.2, 5.2))
            self.line_color.setFill()
            dot.fill()
        except Exception as e:
            print("ChartView drawRect_ error:", e, file=sys.stderr)
            import traceback
            traceback.print_exc()


class _SegmentAction(NSObject):
    """NSObject 子类专用于响应分段控件 action，解决 PyObjC target/action 桥接问题。"""
    def initWithDetails_(self, details):
        self = self.init()
        if self is None:
            return None
        self._details = details
        return self

    def graphMetricChanged_(self, sender):
        self._details.graphMetricChanged_(sender)


class DetailsWindow:

    def __init__(self):
        self.window = None
        self.rows = {}            # key -> value NSTextField
        self.proc_rows = []       # (name_field, value_field, bar_view)
        self.proc_title = None    # 进程排行卡片标题（随曲线指标变化）
        self.graph = None
        self.seg = None
        self.stats = {}           # 当前/峰值/均值 value fields
        self.uptime = None
        self.alert_threshold = 90.0
        self._metric = "cpu"
        self._last = None         # 最近一次 update 的快照，用于即时重绘
        self._created = False
        self._start_ts = time.time()

    # ---------- 基础控件 ----------

    def _text(self, parent, x, y, w, text, size=12, bold=False, mono=False,
              align=NSTextAlignmentLeft, color=None, h=20):
        lab = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        lab.setStringValue_(text)
        lab.setBezeled_(False)
        lab.setDrawsBackground_(False)
        lab.setEditable_(False)
        lab.setSelectable_(False)
        lab.setFont_(_labeled_font(size, bold, mono))
        lab.setAlignment_(align)
        if color is None:
            color = NSColor.labelColor()
        lab.setTextColor_(color)
        parent.addSubview_(lab)
        return lab

    def _card_title(self, card, title, H, x=14):
        lab = self._text(card, x, H - 26, card.frame().size.width - 28, title,
                         size=11, bold=True, color=NSColor.secondaryLabelColor())
        return lab

    # ---------- 构建 ----------

    def _create(self):
        from AppKit import (NSWindow, NSFloatingWindowLevel, NSBackingStoreBuffered,
                            NSButton, NSApplication)
        from Foundation import NSMakeRect

        W = 760
        titlebar = 28
        PAD = 16
        GAP = 12

        A_H = 206          # 曲线卡片
        B_H = 166          # 概览卡片
        C_H = 250          # 温度/进程卡片
        FOOT = 26

        content_h = PAD + A_H + GAP + B_H + GAP + C_H + GAP + FOOT + 4
        H = titlebar + content_h

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskFullSizeContentView,
            NSBackingStoreBuffered, False)
        win.setLevel_(NSFloatingWindowLevel + 1)
        win.setOpaque_(False)
        win.setTitlebarAppearsTransparent_(True)
        win.setTitleVisibility_(1)          # 隐藏标题文字，保留红绿灯
        win.setBackgroundColor_(NSColor.windowBackgroundColor())
        win.setMovableByWindowBackground_(True)

        cv = win.contentView()
        cv.setWantsLayer_(True)

        # 背景（只圆底部两角，避免裁掉标题栏红绿灯）
        bg = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, content_h))
        bg.setWantsLayer_(True)
        layer = bg.layer()
        layer.setCornerRadius_(12.0)
        layer.setMasksToBounds_(True)
        layer.setMaskedCorners_(3)          # 底部左 + 底部右
        layer.setBackgroundColor_(
            NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.96).CGColor())
        cv.addSubview_(bg)

        y = content_h - PAD

        # ---------- 卡片 A：实时曲线 ----------
        cardA = CardView.alloc().initWithFrame_(
            NSMakeRect(PAD, y - A_H, W - 2 * PAD, A_H))
        bg.addSubview_(cardA)
        self._card_title(cardA, "实时曲线", A_H)

        self._seg_handler = _SegmentAction.alloc().initWithDetails_(self)
        seg = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(W - 2 * PAD - 14 - 236, A_H - 34, 236, 22))
        seg.setSegmentCount_(len(GRAPH_METRICS))
        for i, (key, label, _unit) in enumerate(GRAPH_METRICS):
            seg.setLabel_forSegment_(label, i)
            seg.setWidth_forSegment_(59, i)
            seg.setSelected_forSegment_(key == self._metric, i)
        seg.setTarget_(self._seg_handler)
        seg.setAction_("graphMetricChanged:")
        cardA.addSubview_(seg)
        self.seg = seg

        self.graph = ChartView.alloc().initWithFrame_(
            NSMakeRect(14, A_H - 150, W - 2 * PAD - 28, 96))
        cardA.addSubview_(self.graph)

        # 统计行：当前 / 峰值 / 平均（标签在上、数值在下，避免重叠）
        stat_w = (W - 2 * PAD - 28) / 3.0
        for i, (key, label) in enumerate([("cur", "当前"), ("peak", "峰值"), ("avg", "平均")]):
            x = 14 + i * stat_w
            # 标签：y=10（顶部），小字灰色
            self._text(cardA, x, 10, stat_w, label, size=10,
                       color=NSColor.secondaryLabelColor())
            # 数值：y=28（标签下方），大字等宽字体
            self.stats[key] = self._text(
                cardA, x, 28, stat_w, "--", size=17, bold=True, mono=True)

        # ---------- 卡片 B：系统概览（双列） ----------
        y -= A_H + GAP
        cardB = CardView.alloc().initWithFrame_(
            NSMakeRect(PAD, y - B_H, W - 2 * PAD, B_H))
        bg.addSubview_(cardB)
        self._card_title(cardB, "系统概览", B_H)
        col_w = (W - 2 * PAD - 28 - GAP) / 2.0
        rows_b = ["cpu", "freq", "mem", "swap", "disk", "diskio",
                  "net", "battery", "gpu", "gpu2"]
        for i, key in enumerate(rows_b):
            col, row = i // 5, i % 5
            x = 14 + col * (col_w + GAP)
            yy = B_H - 41 - row * 25
            self._text(cardB, x, yy, 92, ROW_TITLES.get(key, key), size=12,
                       color=NSColor.secondaryLabelColor())
            self.rows[key] = self._text(
                cardB, x + 92, yy, col_w - 94, "--", size=12, mono=True,
                align=NSTextAlignmentRight)

        # ---------- 卡片 C1：温度与风扇 ----------
        y -= B_H + GAP
        c1_w = 268
        cardC1 = CardView.alloc().initWithFrame_(NSMakeRect(PAD, y - C_H, c1_w, C_H))
        bg.addSubview_(cardC1)
        self._card_title(cardC1, "温度与风扇", C_H)
        rows_c = ["t_cpu", "t_pipe", "t_gpu", "t_ssd", "t_bat", "t_airport",
                  "fan", "thermal"]
        for i, key in enumerate(rows_c):
            yy = C_H - 44 - i * 24
            self._text(cardC1, 14, yy, 110, ROW_TITLES.get(key, key), size=12,
                       color=NSColor.secondaryLabelColor())
            self.rows[key] = self._text(
                cardC1, 124, yy, c1_w - 138, "--", size=12, mono=True,
                align=NSTextAlignmentRight)

        # ---------- 卡片 C2：CPU 占用 Top 5 ----------
        c2_x = PAD + c1_w + GAP
        c2_w = W - PAD - c2_x
        cardC2 = CardView.alloc().initWithFrame_(NSMakeRect(c2_x, y - C_H, c2_w, C_H))
        bg.addSubview_(cardC2)
        self.proc_title = self._card_title(cardC2, "CPU 占用 Top 5", C_H)
        # 进程名列尽量占满；数值列要容纳 "1023M"/"380.2%"，占比条仅作点缀
        val_w = 52
        bar_w = PROC_BAR_W
        name_w = c2_w - 28 - val_w - bar_w - 8 - 4
        for i in range(5):
            yy = C_H - 46 - i * 30
            name_lab = self._text(cardC2, 14, yy, name_w, "", size=12)
            val_lab = self._text(cardC2, 14 + name_w + 4, yy, val_w, "", size=12,
                                 mono=True, align=2)
            bar_x = 14 + name_w + 4 + val_w + 4
            bar = NSView.alloc().initWithFrame_(
                NSMakeRect(bar_x, yy + 5, 2, 8))
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(2.0)
            bar.layer().setBackgroundColor_(NSColor.systemBlueColor().CGColor())
            self.proc_rows.append((name_lab, val_lab, bar))
            cardC2.addSubview_(bar)
        self._text(cardC2, 14, 12, c2_w - 28, "仅窗口打开时采集", size=10,
                   color=NSColor.tertiaryLabelColor())

        # ---------- 底部 ----------
        self.uptime = self._text(bg, PAD + 2, 8, 300, "", size=10,
                                 color=NSColor.tertiaryLabelColor())
        self._text(bg, W - PAD - 2 - 200, 8, 200, "Esc 关闭窗口", size=10,
                   align=NSTextAlignmentRight, color=NSColor.tertiaryLabelColor())

        # Esc 关闭（1x1 不可见按钮携带键等价，不隐藏以接收按键）
        esc = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        esc.setKeyEquivalent_("\x1b")
        esc.setKeyEquivalentModifierMask_(0)
        esc.setTarget_(win)
        esc.setAction_("orderOut:")
        esc.setHidden_(False)
        bg.addSubview_(esc)

        self.window = win
        self._created = True

    # ---------- 交互 ----------

    def graphMetricChanged_(self, sender):
        try:
            idx = sender.selectedSegment()
            if 0 <= idx < len(GRAPH_METRICS):
                self._metric = GRAPH_METRICS[idx][0]
        except Exception as e:
            print("segmented control error:", e, file=sys.stderr)
        # 曲线与进程排行立即切换，不等下一次刷新
        if self._last is not None:
            self.update(self._last)

    def show(self):
        if not self._created:
            self._create()
        self.window.center()
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def hide(self):
        if self.window:
            self.window.orderOut_(None)

    def is_visible(self):
        return self.window is not None and self.window.isVisible()

    # ---------- 数据刷新 ----------

    def update(self, s):
        if not self._created:
            return
        self._last = s
        try:
            t = s.get("temps", {})
            pm = s.get("pm", {})
            peaks = s.get("peaks", {})

            def f(v, fmt="{:.1f}", suf=""):
                return fmt.format(v) + suf if isinstance(v, (int, float)) else "--"

            def setv(key, text):
                lab = self.rows.get(key)
                if lab is not None:
                    lab.setStringValue_(text)

            # ---- 曲线 ----
            key, label, unit = next(
                m for m in GRAPH_METRICS if m[0] == self._metric)
            data = (s.get("hist") or {}).get(key) or []
            color = _sys_color(GRAPH_COLORS.get(key, "systemBlueColor"))
            threshold = self.alert_threshold if key == "temp" else None
            self.graph.setData_color_threshold_(data, color, threshold)

            vals = [v for v in data if isinstance(v, (int, float))]
            cur = vals[-1] if vals else None
            peak = max(vals) if vals else None
            avg = (sum(vals) / len(vals)) if vals else None
            fmt = "{:.0f}"
            self.stats["cur"].setStringValue_(f(cur, fmt, " " + unit))
            self.stats["peak"].setStringValue_(f(peak, fmt, " " + unit))
            self.stats["avg"].setStringValue_(f(avg, fmt, " " + unit))

            # ---- 概览 ----
            setv("cpu", "{}%  ({} 核)".format(f(s.get("cpu")), s.get("cpu_cores", "--")))
            if isinstance(pm.get("p_freq"), (int, float)) and isinstance(pm.get("e_freq"), (int, float)):
                freq_txt = "P {:.0f} / E {:.0f} MHz".format(pm["p_freq"], pm["e_freq"])
            else:
                freq_txt = "--"
            setv("freq", freq_txt)

            setv("mem", "{}%  ({} / {} MB)".format(
                f(s.get("mem_percent")), s.get("mem_used_mb", "--"), s.get("mem_total_mb", "--")))
            if isinstance(s.get("swap_total_mb"), int) and s.get("swap_total_mb", 0) > 0:
                setv("swap", "{} / {} MB".format(s.get("swap_used_mb", 0), s["swap_total_mb"]))
            else:
                setv("swap", "未使用")

            setv("disk", "{}%  ({} / {} GB)".format(
                f(s.get("disk_percent")), s.get("disk_used_gb", "--"), s.get("disk_total_gb", "--")))
            setv("diskio", "↓{:.1f} ↑{:.1f} MB/s".format(
                s.get("dio_read_mb", 0) or 0, s.get("dio_write_mb", 0) or 0))
            setv("net", "↑{:.2f} ↓{:.2f} MB/s".format(
                s.get("net_up_mb", 0) or 0, s.get("net_down_mb", 0) or 0))

            if isinstance(s.get("battery_percent"), (int, float)):
                bat = "{:.0f}%".format(s["battery_percent"])
                bat += " 充电中" if s.get("battery_plugged") else " 电池供电"
            else:
                bat = "--"
            setv("battery", bat)

            setv("gpu", f(pm.get("gpu_usage"), suf=" %"))
            gf = pm.get("gpu_freq") if isinstance(pm.get("gpu_freq"), (int, float)) else 0
            gp = pm.get("gpu_power") if isinstance(pm.get("gpu_power"), (int, float)) else 0
            setv("gpu2", "{:.0f} MHz / {:.0f} mW".format(gf, gp))

            # ---- 温度与风扇 ----
            setv("t_cpu", "均 {}°C / 峰 {}°C".format(f(t.get("cpu_avg")), f(t.get("cpu_max"))))
            setv("t_pipe", f(t.get("heatpipe"), suf=" °C"))
            gh = t.get("gpu_heatsink")
            if isinstance(gh, (int, float)):
                setv("t_gpu", "{:.1f}°C (核心不可用)".format(gh))
            else:
                setv("t_gpu", "--")
            setv("t_ssd", f(t.get("ssd"), suf=" °C"))
            setv("t_bat", f(t.get("battery"), suf=" °C"))
            setv("t_airport", f(t.get("airport"), suf=" °C"))

            if isinstance(s.get("fan_current"), (int, float)):
                setv("fan", "{:.0f} / {:.0f} RPM".format(s["fan_current"], s.get("fan_max") or 0))
            else:
                setv("fan", "--")
            setv("thermal", str(pm.get("thermal", "--")))

            # ---- 进程排行（随曲线指标联动） ----
            r_title, r_key, r_mode = PROC_RANKING.get(
                self._metric, PROC_RANKING["cpu"])
            if self.proc_title is not None:
                self.proc_title.setStringValue_(r_title)
            procs = s.get(r_key) or []
            values = [(p[1] if r_mode == "cpu" else p[2]) for p in procs]
            max_v = max(values + [1.0])
            base = _sys_color("systemGreenColor" if r_mode == "mem"
                              else "systemBlueColor")
            for i, (name_lab, val_lab, bar) in enumerate(self.proc_rows):
                if i < len(procs):
                    name, cpu, rss = procs[i]
                    v = cpu if r_mode == "cpu" else rss
                    # 按实测像素宽度截断（比按字符数估算准确，尽量展示完整名称）
                    usable = name_lab.frame().size.width - 2
                    name_lab.setStringValue_(
                        _truncate(name, name_lab.font(), usable))
                    val_lab.setStringValue_(_fmt_proc_value(v, r_mode))
                    # 数值列宽固定，多线程进程 CPU 可超 100%、内存可达十几 G，
                    # 装不下时自动缩一号字体，避免右侧被裁切
                    if val_lab.cell().cellSize().width > val_lab.frame().size.width - 1:
                        val_lab.setFont_(_labeled_font(10, mono=True))
                    else:
                        val_lab.setFont_(_labeled_font(12, mono=True))
                    fr = bar.frame()
                    bar.setHidden_(False)
                    bar.setFrame_(NSMakeRect(fr.origin.x, fr.origin.y,
                                   2 + (PROC_BAR_W - 2) * min(v / max_v, 1.0),
                                   fr.size.height))
                    # CPU 按绝对占用告警；内存条是绝对值，按占榜首比例着色
                    if r_mode == "cpu":
                        if v >= 60:
                            c = NSColor.systemRedColor()
                        elif v >= 25:
                            c = NSColor.systemOrangeColor()
                        else:
                            c = base
                    else:
                        share = v / max_v
                        if share >= 0.75:
                            c = NSColor.systemRedColor()
                        elif share >= 0.4:
                            c = NSColor.systemOrangeColor()
                        else:
                            c = base
                    bar.layer().setBackgroundColor_(c.CGColor())
                else:
                    name_lab.setStringValue_("")
                    val_lab.setStringValue_("")
                    bar.setHidden_(True)

            # ---- 底部 ----
            secs = int(time.time() - self._start_ts)
            self.uptime.setStringValue_(
                "会话已运行 {:02d}:{:02d}:{:02d} · 数据每 2 秒刷新".format(
                    secs // 3600, secs % 3600 // 60, secs % 60))

        except Exception as e:
            print("details update error:", e, file=sys.stderr)


# ============================================================
# 主应用
# ============================================================

# ============================================================
# 主应用
# ============================================================

# (config key, 菜单标签, 取值函数名说明)
TITLE_METRICS = [
    ("mem", "内存"),
    ("cpu", "CPU"),
    ("disk", "磁盘"),
    ("gpu", "GPU"),
    ("temp", "CPU 温度"),
    ("fan", "风扇"),
    ("net", "下载速度"),
    ("battery", "电池"),
]

PM_INTERVALS = [0, 2, 5, 8, 15]
PM_LABELS = {0: "关闭", 2: "2 秒", 5: "5 秒", 8: "8 秒", 15: "15 秒"}
TEMP_THRESHOLDS = [85, 90, 95, 100]


class SystemMonitorApp(rumps.App):
    def __init__(self):
        super().__init__("MacResourceMonitor", title="…", icon=None,
                         quit_button="Quit")

        # 设为辅助应用：不占 Dock
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

        self.config = load_config()
        self._alert_last = {}
        self._debug = os.environ.get("MRM_DEBUG") == "1"
        self._refresh_count = 0

        # ---- 菜单 ----
        self.show_item = rumps.MenuItem("显示详情…", callback=self.toggle_details)

        self.title_menu = rumps.MenuItem("菜单栏显示")
        self._title_items = {}
        for key, label in TITLE_METRICS:
            item = rumps.MenuItem(label, callback=self._set_title_metric)
            item._mrm_key = key
            self._title_items[key] = item
            self.title_menu.add(item)

        self.pm_menu = rumps.MenuItem("GPU 数据采样")
        self._pm_items = {}
        for sec in PM_INTERVALS:
            item = rumps.MenuItem(PM_LABELS[sec], callback=self._set_pm_interval)
            item._mrm_key = sec
            self._pm_items[sec] = item
            self.pm_menu.add(item)

        self.alert_menu = rumps.MenuItem("温度告警")
        self.alert_toggle = rumps.MenuItem("启用告警", callback=self._toggle_alerts)
        self.alert_menu.add(self.alert_toggle)
        self.alert_menu.add(None)
        self._th_items = {}
        for th in TEMP_THRESHOLDS:
            item = rumps.MenuItem("{}°C".format(th), callback=self._set_threshold)
            item._mrm_key = th
            self._th_items[th] = item
            self.alert_menu.add(item)

        self.health_item = rumps.MenuItem("数据源: --")
        self.force_pm_item = rumps.MenuItem("立即刷新 GPU/CPU 数据", callback=self._force_pm)

        self.cpu_item = rumps.MenuItem("CPU: --")
        self.mem_item = rumps.MenuItem("内存: --")
        self.disk_item = rumps.MenuItem("磁盘: --")
        self.gpu_item = rumps.MenuItem("GPU: --")
        self.temp_item = rumps.MenuItem("温度: --")
        self.fan_item = rumps.MenuItem("风扇: --")
        self.net_item = rumps.MenuItem("网络: --")

        self.menu = [
            self.show_item,
            self.title_menu,
            self.pm_menu,
            self.alert_menu,
            self.force_pm_item,
            None,
            self.health_item,
            None,
            self.cpu_item,
            self.mem_item,
            self.disk_item,
            self.gpu_item,
            self.temp_item,
            self.fan_item,
            self.net_item,
        ]

        self._sync_menu_states()

        self.details = DetailsWindow()
        self.details.alert_threshold = float(self.config.get("temp_threshold", 90))
        self.collector = Collector(interval=2.0, config=self.config)
        self.collector.start()

        self.timer = rumps.Timer(self.refresh, 1.0)
        self.timer.start()

    # ---- 菜单回调 ----

    def _sync_menu_states(self):
        for key, item in self._title_items.items():
            item.state = 1 if key == self.config.get("title_metric") else 0
        for sec, item in self._pm_items.items():
            item.state = 1 if float(sec) == float(self.config.get("pm_interval", 8)) else 0
        for th, item in self._th_items.items():
            item.state = 1 if float(th) == float(self.config.get("temp_threshold", 90)) else 0
        self.alert_toggle.state = 1 if self.config.get("alerts_enabled") else 0

    def _set_title_metric(self, sender):
        key = getattr(sender, "_mrm_key", None)
        if not key:
            return
        self.config["title_metric"] = key
        save_config(self.config)
        self._sync_menu_states()

    def _set_pm_interval(self, sender):
        sec = getattr(sender, "_mrm_key", None)
        if sec is None:
            return
        self.config["pm_interval"] = float(sec)
        save_config(self.config)
        if sec == 0:
            self._alert_last.pop("thermal", None)
        self._sync_menu_states()

    def _toggle_alerts(self, sender):
        self.config["alerts_enabled"] = not self.config.get("alerts_enabled", True)
        save_config(self.config)
        self._sync_menu_states()

    def _set_threshold(self, sender):
        th = getattr(sender, "_mrm_key", None)
        if th is None:
            return
        self.config["temp_threshold"] = float(th)
        self.details.alert_threshold = float(th)
        save_config(self.config)
        self._sync_menu_states()

    def _force_pm(self, _):
        self.collector.force_pm()

    def toggle_details(self, _):
        if self.details.is_visible():
            self.details.hide()
            self.collector.set_track_processes(False)
        else:
            self.details.show()
            self.collector.set_track_processes(True)
            self.details.update(self.collector.get())

    # ---- 告警 ----

    def _fire_alert(self, key, title, message, cooldown=300.0):
        now = time.time()
        if now - self._alert_last.get(key, 0.0) < cooldown:
            return
        self._alert_last[key] = now
        try:
            rumps.notification(APP_NAME, title, message)
        except Exception as e:
            print("notification error:", e, file=sys.stderr)

    def _check_alerts(self, s):
        if not self.config.get("alerts_enabled", True):
            return
        t = s.get("temps", {})
        cpu_avg = t.get("cpu_avg")
        threshold = float(self.config.get("temp_threshold", 90))
        if isinstance(cpu_avg, (int, float)) and cpu_avg >= threshold:
            self._fire_alert(
                "temp", "CPU 温度过高",
                "当前 {:.1f}°C，已超过阈值 {:.0f}°C".format(cpu_avg, threshold))
        if self.config.get("thermal_alert", True):
            level = (s.get("pm", {}).get("thermal") or "").strip()
            if level and level != "Nominal":
                self._fire_alert("thermal", "系统热压力提升",
                                 "当前等级: {}".format(level))

    # ---- 刷新 ----

    def _log_status_item(self):
        """诊断：把状态栏项状态写入日志（仅在 MRM_DEBUG=1 时）"""
        if not self._debug:
            return
        self._refresh_count += 1
        if self._refresh_count % 5 != 0:
            return
        try:
            si = self._nsapp.nsstatusitem
            btn = si.button()
            win = btn.window() if btn else None
            line = "[{:.0f}s] title={!r} itemVisible={} ".format(
                time.time() % 10000, self.title, si.isVisible())
            if win:
                f = win.frame()
                line += "winVisible={} frame=({:.0f},{:.0f},{:.0f}x{:.0f})".format(
                    win.isVisible(), f.origin.x, f.origin.y, f.size.width, f.size.height)
            print(line, flush=True)
        except Exception as e:
            print("status item log error:", e, flush=True)

    def toggle_details_visible(self):
        pass

    def _title_value(self, s):
        """返回 (数值, 格式)。"""
        pm = s.get("pm", {})
        t = s.get("temps", {})
        metric = self.config.get("title_metric", "mem")
        if metric == "cpu":
            return s.get("cpu"), "{:.0f}%"
        if metric == "disk":
            return s.get("disk_percent"), "{:.0f}%"
        if metric == "gpu":
            return pm.get("gpu_usage"), "{:.0f}%"
        if metric == "temp":
            return t.get("cpu_avg"), "{:.0f}°C"
        if metric == "fan":
            return s.get("fan_current"), "{:.0f}rpm"
        if metric == "net":
            return s.get("net_down_mb"), "{:.1f}MB/s"
        if metric == "battery":
            return s.get("battery_percent"), "{:.0f}%"
        return s.get("mem_percent"), "{:.0f}%"

    def refresh(self, sender):
        self._log_status_item()
        s = self.collector.get()
        if not s:
            return
        try:
            pm = s.get("pm", {})
            t = s.get("temps", {})

            cpu = s.get("cpu", 0.0)

            # 菜单栏标题（指标可在菜单中实时切换）
            val, fmt = self._title_value(s)
            self.title = fmt.format(val) if isinstance(val, (int, float)) else "--"

            self.cpu_item.title = "CPU: {:.1f}%".format(cpu)
            self.mem_item.title = "内存: {:.1f}%  ({} / {} MB)".format(
                s.get("mem_percent", 0), s.get("mem_used_mb", "--"), s.get("mem_total_mb", "--"))
            self.disk_item.title = "磁盘: {:.1f}%  ({} / {} GB，可用 {} GB)".format(
                s.get("disk_percent", 0), s.get("disk_used_gb", "--"),
                s.get("disk_total_gb", "--"), s.get("disk_free_gb", "--"))

            if isinstance(pm.get("gpu_usage"), (int, float)):
                self.gpu_item.title = "GPU: {:.1f}%  ({:.0f} MHz)".format(
                    pm["gpu_usage"], pm.get("gpu_freq", 0))
            else:
                note = "已关闭" if not s.get("pm_enabled") else "需 sudo"
                self.gpu_item.title = "GPU: {} ({})".format("--", note)

            if isinstance(t.get("cpu_avg"), (int, float)):
                txt = "CPU 温度: {:.1f}°C".format(t["cpu_avg"])
                if isinstance(t.get("gpu_heatsink"), (int, float)):
                    txt += "  |  GPU散热器: {:.1f}°C".format(t["gpu_heatsink"])
                self.temp_item.title = txt
            else:
                self.temp_item.title = "温度: --"

            if isinstance(s.get("fan_current"), (int, float)):
                self.fan_item.title = "风扇: {:.0f} RPM".format(s["fan_current"])
            else:
                self.fan_item.title = "风扇: --"

            self.net_item.title = "网络: ↑ {:.2f} MB/s  ↓ {:.2f} MB/s".format(
                s.get("net_up_mb", 0) or 0, s.get("net_down_mb", 0) or 0)

            h = s.get("health", {})
            self.health_item.title = "数据源: iSMC {} | GPU {}".format(
                h.get("ismc", "?"), h.get("pm", "?"))

            self._check_alerts(s)

            if self.details.is_visible():
                self.details.update(s)

        except Exception as e:
            print("refresh error:", e, file=sys.stderr)


if __name__ == "__main__":
    app = SystemMonitorApp()
    app.run()
