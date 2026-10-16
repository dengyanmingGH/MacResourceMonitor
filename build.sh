#!/bin/bash
# MacResourceMonitor 构建脚本
set -e
cd "$(dirname "$0")"

APP="dist/MacResourceMonitor.app"
RES="$APP/Contents/Resources"
CACHE="build/lib"

echo "==> 1/6 查找可用的 Python 解释器"

find_py_with_deps() {
    # 已经装了 rumps + psutil 的解释器（可跳过安装）
    for cand in \
        /opt/homebrew/bin/python3.10 \
        /opt/homebrew/bin/python3.11 \
        /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3 \
        /usr/bin/python3 \
        python3; do
        command -v "$cand" >/dev/null 2>&1 || continue
        [ -x "$cand" ] || continue
        if "$cand" -c "import rumps, psutil" 2>/dev/null; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

find_any_py() {
    # 任意 >=3.9 的解释器（用于 pip 安装到 build/lib）
    for cand in \
        /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.11 \
        /opt/homebrew/bin/python3.10 \
        /opt/homebrew/bin/python3 \
        python3 \
        /usr/bin/python3; do
        command -v "$cand" >/dev/null 2>&1 || continue
        [ -x "$cand" ] || continue
        if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

NEED_INSTALL=0
PY="$(find_py_with_deps || true)"
if [ -n "$PY" ]; then
    echo "    使用: $PY ($($PY --version 2>&1))（依赖已就绪）"
else
    PY="$(find_any_py || true)" || true
    if [ -z "$PY" ]; then echo "错误: 未找到可用的 Python (>=3.9)。"; exit 1; fi
    NEED_INSTALL=1
    echo "    使用: $PY ($($PY --version 2>&1))（需安装依赖）"
fi

# 校验 iSMC（温度/风扇数据来源）
if [ -x /opt/homebrew/bin/iSMC ]; then
    echo "    iSMC: /opt/homebrew/bin/iSMC"
else
    echo "    警告: 未找到 iSMC，温度/风扇将不可用 (brew install dkorunic/tap/ismc)"
fi

echo "==> 2/6 准备 Python 依赖（增量缓存到 $CACHE）"
mkdir -p "$CACHE"
if [ "$NEED_INSTALL" = "1" ] || ! PYTHONPATH="$CACHE" "$PY" -c "import rumps, psutil" 2>/dev/null; then
    "$PY" -m pip install --quiet --target "$CACHE" rumps psutil
    find "$CACHE" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
else
    echo "    依赖已就绪，跳过安装"
fi

echo "==> 3/6 创建应用目录结构"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"

echo "==> 4/6 编译 C 启动器"
cc -O2 -o "$APP/Contents/MacOS/MacResourceMonitor" launcher.c

echo "==> 5/6 复制依赖、程序与 Info.plist"
cp -R "$CACHE" "$RES/lib"
cp system_monitor.py "$RES/"
# 解释器路径写入 Resources/，启动器据此启动（带回退链）
echo "$PY" > "$RES/interpreter"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>MacResourceMonitor</string>
    <key>CFBundleDisplayName</key><string>系统资源监控</string>
    <key>CFBundleIdentifier</key><string>com.dengyanming.macresourcemonitor</string>
    <key>CFBundleVersion</key><string>5.0.0</string>
    <key>CFBundleShortVersionString</key><string>5.0.0</string>
    <key>CFBundleExecutable</key><string>MacResourceMonitor</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleSignature</key><string>????</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

echo "==> 6/6 代码签名（ad-hoc）并注册"
codesign --force --sign - "$APP" 2>/dev/null || true
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APP" 2>/dev/null || true

echo ""
echo "构建完成: $APP"
echo "启动: open $APP"
