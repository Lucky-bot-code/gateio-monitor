#!/usr/bin/env python3
"""Gate.io MA10 Monitor — 阿里云一键部署脚本 (paramiko)"""
import os, sys, io, tarfile, time

try:
    import paramiko
except ImportError:
    print("请先安装 paramiko: pip install paramiko")
    sys.exit(1)

# ========== 配置 ==========
SERVER_IP = "116.62.152.64"
SERVER_PORT = 22
SERVER_USER = "root"
SERVER_PASS = "4725036Qq"
APP_DIR = "/opt/gateio-monitor"
SERVICE_NAME = "gateio-monitor"
PORT = 5000

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXCLUDE = {
    ".git", "__pycache__", ".ma10_state.json", "positions.json",
    "price_alerts.json", "short_flip_state.json", "tp_state.json",
    "klines.db", ".claude",
    # 服务器运行时状态，部署时保留
    "gateio_available_symbols.json", "wecom_subscriptions.json",
    "extreme_sent.json",
}

EXCLUDE_PREFIX = ("tmp", "binance_", "finnhub_", "test_")


def _should_include(name):
    """检查文件/目录是否应打包"""
    base = os.path.basename(name)
    if base in EXCLUDE:
        return False
    for prefix in EXCLUDE_PREFIX:
        if base.startswith(prefix):
            return False
    if base.endswith(".tar.gz") or base.endswith(".pyc"):
        return False
    if base == "deploy.py":
        return False
    return True


def create_tarball():
    """创建项目 tar.gz"""
    print("[1/5] 创建项目压缩包...")
    tarball = os.path.join(PROJECT_DIR, "gateio-monitor.tar.gz")

    with tarfile.open(tarball, "w:gz") as tar:
        for fname in sorted(os.listdir(PROJECT_DIR)):
            if not _should_include(fname):
                print(f"  排除: {fname}")
                continue
            full = os.path.join(PROJECT_DIR, fname)
            if os.path.isdir(full):
                for root, dirs, files in os.walk(full):
                    dirs[:] = [d for d in dirs if _should_include(d)]
                    for fn in files:
                        if not _should_include(fn):
                            continue
                        fp = os.path.join(root, fn)
                        arcname = os.path.relpath(fp, PROJECT_DIR)
                        tar.add(fp, arcname=arcname)
            else:
                tar.add(full, arcname=fname)

    size_mb = os.path.getsize(tarball) / 1024 / 1024
    print(f"  压缩完成: {tarball} ({size_mb:.1f} MB)")
    return tarball


def deploy():
    tarball = create_tarball()
    tarball_name = os.path.basename(tarball)

    print(f"[2/5] 连接服务器 {SERVER_USER}@{SERVER_IP}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SERVER_IP, SERVER_PORT, SERVER_USER, SERVER_PASS, timeout=30)
    print("  SSH 连接成功")

    sftp = ssh.open_sftp()

    # 上传
    print("[3/5] 上传项目文件...")
    remote_tar = f"/tmp/{tarball_name}"
    sftp.put(tarball, remote_tar, callback=lambda sent, total: print(
        f"\r  上传: {sent/1024/1024:.1f}/{total/1024/1024:.1f} MB", end="", flush=True
    ))
    print()
    sftp.close()

    # 服务器端部署
    print("[4/5] 服务器端部署...")

    cmds = f"""
set -e
# 停止旧服务
systemctl stop {SERVICE_NAME} 2>/dev/null || true

# 备份运行时状态文件
mkdir -p /tmp/gateio-state
for f in gateio_available_symbols.json wecom_subscriptions.json extreme_sent.json .ma10_state.json positions.json price_alerts.json short_flip_state.json tp_state.json klines.db; do
    [ -f {APP_DIR}/$f ] && cp {APP_DIR}/$f /tmp/gateio-state/ || true
done

# 清理旧目录、解压新文件
rm -rf {APP_DIR}
mkdir -p {APP_DIR}
tar xzf {remote_tar} -C {APP_DIR}
rm -f {remote_tar}

# 恢复运行时状态文件
[ -n "$(ls -A /tmp/gateio-state 2>/dev/null)" ] && cp /tmp/gateio-state/* {APP_DIR}/ || true
rm -rf /tmp/gateio-state

# 安装依赖
pip3 install flask requests PySocks -q 2>&1 | tail -1

# 安装 systemd 服务
cp {APP_DIR}/deploy/gateio-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable {SERVICE_NAME}

# 启动服务
systemctl restart {SERVICE_NAME}
sleep 3
systemctl status {SERVICE_NAME} --no-pager
"""
    stdin, stdout, stderr = ssh.exec_command(cmds)
    for line in stdout:
        print(f"  {line.strip()}")
    for line in stderr:
        print(f"  [stderr] {line.strip()}")

    print(f"\n[5/5] 部署完成!")
    print(f"  访问地址: http://{SERVER_IP}:{PORT}")
    print(f"  查看日志: ssh root@{SERVER_IP} journalctl -u {SERVICE_NAME} -f")

    ssh.close()
    print("完成。")


if __name__ == "__main__":
    deploy()
