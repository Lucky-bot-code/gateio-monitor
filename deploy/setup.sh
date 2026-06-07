#!/bin/bash
# Gate.io MA10 Monitor — 阿里云一键部署脚本
# 用法: chmod +x setup.sh && bash setup.sh

set -e

APP_DIR="/opt/gateio-monitor"
echo "=== Gate.io MA10 Monitor 部署 ==="

# 1. 安装依赖
echo "[1/4] 安装 Python 依赖..."
pip3 install flask requests PySocks -q

# 2. 创建目录并拉代码（如果目录已存在则跳过 git clone）
if [ ! -d "$APP_DIR" ]; then
    echo "[2/4] 克隆项目..."
    git clone https://github.com/Lucky-bot-code/gateio-monitor.git "$APP_DIR"
else
    echo "[2/4] 项目目录已存在，拉取最新代码..."
    cd "$APP_DIR" && git pull
fi

# 3. 安装 systemd 服务
echo "[3/4] 安装 systemd 服务..."
cp "$APP_DIR/deploy/gateio-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable gateio-monitor

# 4. 启动服务
echo "[4/4] 启动服务..."
systemctl restart gateio-monitor
sleep 3
systemctl status gateio-monitor --no-pager

# 完成
IP=$(curl -s ifconfig.me 2>/dev/null || echo "服务器IP")
echo ""
echo "============================================"
echo "  部署完成！"
echo "  访问地址: http://${IP}:5000"
echo "  查看日志: journalctl -u gateio-monitor -f"
echo "  重启服务: systemctl restart gateio-monitor"
echo "============================================"
echo ""
echo "⚠  别忘了去阿里云安全组开放 5000 端口（TCP, 0.0.0.0/0）"
