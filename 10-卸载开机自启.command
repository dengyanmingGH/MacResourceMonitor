#!/bin/bash
# 双击此文件：关闭「系统资源监控」开机自启

PLIST_DST="$HOME/Library/LaunchAgents/com.dengyanming.macresourcemonitor.plist"
LABEL="com.dengyanming.macresourcemonitor"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
pkill -f "MacResourceMonitorFixed.*system_monitor.py" 2>/dev/null
sleep 1
rm -f "$PLIST_DST"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "⚠️  卸载可能未完全生效"
else
    echo "✅ 开机自启已关闭，监控已停止"
fi

echo ""
echo "（本窗口可以关闭）"
