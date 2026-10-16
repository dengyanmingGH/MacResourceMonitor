#!/bin/bash
# 双击此文件即可启动「系统资源监控」
# 使用方法：在 Finder 中双击本文件

APP="/Users/dengyanming/Work/DshWork/MacResourceMonitorFixed/dist/MacResourceMonitor.app"

# 如果已有实例，先停止
if pgrep -f "MacResourceMonitorFixed.*system_monitor.py" > /dev/null 2>&1; then
    echo "正在停止已有实例..."
    pkill -f "MacResourceMonitorFixed.*system_monitor.py"
    sleep 2
fi

echo "正在启动「系统资源监控」..."
open "$APP"

sleep 3

if pgrep -f "MacResourceMonitorFixed.*system_monitor.py" > /dev/null 2>&1; then
    echo ""
    echo "✅ 启动成功！"
    echo ""
    echo "  菜单栏右上角会出现内存使用百分比。"
    echo "  点击它可查看：CPU / 内存 / 磁盘 / GPU / 温度 / 风扇 / 网络"
    echo "  选择「显示详情…」可打开详细面板（历史曲线 / 峰值 / Top5 进程）"
    echo "  在「菜单栏显示」子菜单里可实时切换菜单栏指标"
    echo ""
    echo "  如需停止：双击「停止监控.command」"
else
    echo "❌ 启动失败，请查看日志：/tmp/macresourcemonitor.log"
fi

echo ""
echo "（本窗口可以关闭）"
