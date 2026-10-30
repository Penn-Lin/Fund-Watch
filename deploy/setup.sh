#!/usr/bin/env bash
# 基金涨跌监控 —— 阿里云 Ubuntu 24.04 初始化脚本
# 用法：sudo bash setup.sh [公网IP]
#   带 IP   → 走 HTTPS（443，LE 短周期 IP 证书），同时保底开 8443 HTTP
#   不带 IP → 只开 8443 HTTP，等备案/证书就绪后再跑一次带 IP 的
# 幂等：可以重复跑，已存在的用户/库/环境不会重建
set -euo pipefail

PUBLIC_IP="${1:-}"
if [ -n "$PUBLIC_IP" ] && ! printf '%s' "$PUBLIC_IP" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
    echo "[ERROR] 参数不是合法 IPv4：$PUBLIC_IP" >&2
    exit 1
fi

APP_DIR=/opt/fund-monitor
APP_USER=fund
DB_NAME=fund
DB_USER=fund

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[ERROR] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行"

log "1/8 系统时区与基础包"
timedatectl set-timezone Asia/Shanghai
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3-venv python3-pip python3-dev \
    build-essential libpq-dev \
    postgresql postgresql-contrib \
    git curl ca-certificates rsync ufw

log "2/8 安装 Caddy（官方源）"
if ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
fi
caddy version

log "3/8 创建运行用户 $APP_USER"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

log "4/8 初始化 PostgreSQL"
systemctl enable --now postgresql

# 2G 小机加 2G swap：pip 装 cryptography / 编译 psycopg2 时容易顶到 OOM
if ! swapon --show | grep -q .; then
    if [ ! -f /swapfile ]; then
        fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
    fi
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -qw vm.swappiness=10
    echo "已启用 2G swap"
fi

DB_PASS_FILE=/root/.fund_monitor_db_pass
if [ -f "$DB_PASS_FILE" ]; then
    DB_PASS=$(cat "$DB_PASS_FILE")
    echo "复用已有数据库密码（$DB_PASS_FILE）"
else
    DB_PASS=$(openssl rand -hex 16)
    echo "$DB_PASS" > "$DB_PASS_FILE"
    chmod 600 "$DB_PASS_FILE"
    echo "新生成数据库密码，已存至 $DB_PASS_FILE"
fi

sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"

sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

# 2G 内存的调优：默认 shared_buffers=128MB 可用，但 effective_cache_size
# 默认按大内存机器给 4GB，会让规划器高估缓存命中、选错执行计划。
sudo -u postgres psql -q <<'SQL'
ALTER SYSTEM SET shared_buffers = '256MB';
ALTER SYSTEM SET effective_cache_size = '768MB';
ALTER SYSTEM SET work_mem = '8MB';
ALTER SYSTEM SET maintenance_work_mem = '64MB';
ALTER SYSTEM SET max_connections = 50;
SQL
systemctl restart postgresql

log "5/8 检查代码是否已上传"
[ -f "$APP_DIR/app.py" ] || die "没找到 $APP_DIR/app.py。请先把项目文件上传到 $APP_DIR（见对话里的 rsync 命令）"
[ -f "$APP_DIR/requirements.txt" ] || die "没找到 $APP_DIR/requirements.txt"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "6/8 创建 venv 并安装依赖"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -c "import flask, psycopg2, cryptography; print('依赖 OK')"

log "7/8 写入 .env"
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    {
        echo "DATABASE_URL=postgresql://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME"
        echo "TZ=Asia/Shanghai"
        if [ -n "$PUBLIC_IP" ]; then
            echo "VAPID_SUBJECT=https://$PUBLIC_IP"
        fi
    } > "$ENV_FILE"
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "已生成 $ENV_FILE"
else
    # 补 TZ（老 .env 可能没写，缺了交易日判断会错位）
    grep -q '^TZ=' "$ENV_FILE" || echo 'TZ=Asia/Shanghai' >> "$ENV_FILE"
    echo "$ENV_FILE 已存在，保留不动"
fi

log "8/8 安装 systemd 服务"
install -m 644 "$APP_DIR/deploy/fund-monitor.service" /etc/systemd/system/fund-monitor.service
systemctl daemon-reload
systemctl enable fund-monitor

# 日志上限，避免 journald 把 40G 系统盘写满
if ! grep -q '^SystemMaxUse=' /etc/systemd/journald.conf; then
    sed -i 's/^\[Journal\]/[Journal]\nSystemMaxUse=200M/' /etc/systemd/journald.conf
    systemctl restart systemd-journald
fi

log "Caddy 配置：先把 8443 HTTP 跑通，再决定是否上 HTTPS"
mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy 2>/dev/null || true

# 记录上一次用的 IP，避免「之前带 IP 跑过、这次忘带」把 HTTPS 服务弄掉
LAST_IP_FILE=/etc/fund-monitor.public-ip
if [ -z "$PUBLIC_IP" ] && [ -f "$LAST_IP_FILE" ]; then
    PUBLIC_IP=$(cat "$LAST_IP_FILE")
    echo "沿用上次记录的公网 IP：$PUBLIC_IP"
fi

if [ -n "$PUBLIC_IP" ]; then
    sed "s/__PUBLIC_IP__/$PUBLIC_IP/" "$APP_DIR/deploy/Caddyfile.ip-https" > /etc/caddy/Caddyfile
    echo "$PUBLIC_IP" > "$LAST_IP_FILE"
    echo "已生成 HTTPS 配置（$PUBLIC_IP，443）+ 保底 8443"
else
    install -m 644 "$APP_DIR/deploy/Caddyfile.http" /etc/caddy/Caddyfile
    echo "未提供公网 IP，仅开 8443 HTTP。拿到 IP 后重跑：sudo bash deploy/setup.sh <公网IP>"
fi

caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 \
    || { caddy adapt --config /etc/caddy/Caddyfile >/dev/null || die "Caddyfile 语法不通过"; }
# caddy validate 以 root 身份会顺手把 log 文件建成 root:root 0600，
# 之后服务以 caddy 用户跑就会 permission denied，必须把属主改回来
chown -R caddy:caddy /var/log/caddy
systemctl enable --now caddy
systemctl restart caddy

log "防火墙：放行 22 / 8443 / 443（阿里云安全组还要另外加）"
ufw allow 22/tcp   >/dev/null 2>&1 || true
ufw allow 8443/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp  >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

log "启动应用"
systemctl restart fund-monitor
sleep 3
systemctl is-active --quiet fund-monitor || {
    journalctl -u fund-monitor -n 40 --no-pager
    die "fund-monitor 没起来，看上面的日志"
}

cat <<EOF

────────────────────────────────────────
初始化完成。数据库密码：$DB_PASS
（也保存在 $DB_PASS_FILE）

自测：
  systemctl status fund-monitor
  journalctl -u fund-monitor -f
  curl -s localhost:8000/api/version
EOF

if [ -n "$PUBLIC_IP" ]; then
    cat <<EOF
  curl -s https://$PUBLIC_IP/api/version      # 443 证书生效后可用
  curl -sk http://$PUBLIC_IP:8443/api/version # 保底入口
EOF
else
    echo "  curl -s http://<公网IP>:8443/api/version"
fi

cat <<'EOF'

注意：venv 里的 Python 是 3.12，和 render.yaml 里写的 3.11.9 不同，
依赖都有 cp312 预编译包，不需要改代码。
────────────────────────────────────────
EOF
