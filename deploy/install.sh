#!/bin/bash
# ASMR WebUI 一键部署脚本（在 NAS 上运行）
set -e

echo "=== ASMR WebUI 部署 ==="

# 1. 安装 Flask（系统 Python + break-system-packages）
echo "[1/5] 安装 Flask..."
echo 'NAS@wwx061120' | sudo -S pip3 install --break-system-packages 'flask>=3.0,<4.0' 2>&1 | tail -3

# 2. 创建目录
echo "[2/5] 创建目录..."
echo 'NAS@wwx061120' | sudo -S mkdir -p /opt/asmr-webui /vol1/asmr-downloads /vol1/asmr-data
echo 'NAS@wwx061120' | sudo -S chown -R wwx:wwx /opt/asmr-webui /vol1/asmr-downloads /vol1/asmr-data

# 3. 复制代码
echo "[3/5] 复制代码..."
echo 'NAS@wwx061120' | sudo -S cp -r . /opt/asmr-webui/
echo 'NAS@wwx061120' | sudo -S chown -R wwx:wwx /opt/asmr-webui

# 4. 创建 systemd service
echo "[4/5] 注册 systemd 服务..."
echo 'NAS@wwx061120' | sudo -S cp deploy/asmr-webui.service /etc/systemd/system/
echo 'NAS@wwx061120' | sudo -S systemctl daemon-reload
echo 'NAS@wwx061120' | sudo -S systemctl enable asmr-webui.service

# 5. 启动服务
echo "[5/5] 启动服务..."
echo 'NAS@wwx061120' | sudo -S systemctl restart asmr-webui.service
sleep 2
echo 'NAS@wwx061120' | sudo -S systemctl status asmr-webui.service --no-pager -l || true

echo ""
echo "========================================="
echo "✅ 部署完成！"
echo "  WebUI: http://192.168.1.8:8787"
echo "  查看日志: sudo journalctl -u asmr-webui -f"
echo "  停止: sudo systemctl stop asmr-webui"
echo "  启动: sudo systemctl start asmr-webui"
echo "  重启: sudo systemctl restart asmr-webui"
echo "  状态: sudo systemctl status asmr-webui"
echo "========================================="
