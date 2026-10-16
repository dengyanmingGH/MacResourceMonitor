#!/bin/bash
# 双击此文件：开启「系统资源监控」开机自启

PLIST_SRC="/Users/dengyanming/Work/DshWork/MacResourceMonitorFixed/autostart.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.dengyanming.macresourcemonitor.plist"
LABEL="com.dengyanming.macresourcemonitor"

mkdir -p "$HOME/Library/LaunchAgents"

# 先卸载旧的
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
sleep 1

cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>&1 | head -3

sleep 4

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "✅ 开机自启已开启"
    echo ""
    echo "  配置位置：$PLIST_DST"
    echo "  登录后会自动启动，若意外退出会自动重启。"
    echo ""
    echo "  关闭：双击「10-卸载开机自启.command」"
else
    echo "❌ 开启失败"
fi

echo ""
echo "（本窗口可以关闭）"
