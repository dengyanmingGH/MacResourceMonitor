#!/bin/bash
# 双击此文件即可停止「系统资源监控」

if pgrep -f "MacResourceMonitorFixed.*system_monitor.py" > /dev/null 2>&1; then
    pkill -f "MacResourceMonitorFixed.*system_monitor.py"
    sleep 1
    echo "✅ 监控已停止"
else
    echo "监控未在运行"
fi

echo ""
echo "（本窗口可以关闭）"
