#!/usr/bin/env bash
# 服务器体检：这台机器上装了什么、跑得怎么样，一屏看完
# 用法：bash /opt/fund-monitor/deploy/status.sh
set -uo pipefail

APP_DIR=/opt/fund-monitor
line() { printf '\n\033[1;36m── %s ─────────────────────────\033[0m\n' "$*"; }

line "系统"
. /etc/os-release 2>/dev/null
echo "OS      : ${PRETTY_NAME:-unknown}"
echo "内核    : $(uname -r)"
echo "CPU     : $(nproc) 核  负载: $(cut -d' ' -f1-3 /proc/loadavg)"
echo "内存    : $(free -h | awk '/^Mem:/{print $3" / "$2}')  可用: $(free -h | awk '/^Mem:/{print $7}')"
echo "Swap    : $(free -h | awk '/^Swap:/{print $3" / "$2}')"
echo "磁盘 /  : $(df -h / | awk 'NR==2{print $3" / "$2" ("$5" 已用)"}')"
echo "运行时长: $(uptime -p)"
echo "时区    : $(timedatectl show -p Timezone --value)"

line "服务状态"
for s in fund-monitor caddy postgresql; do
    printf '%-14s %s\n' "$s" "$(systemctl is-active $s 2>/dev/null) / $(systemctl is-enabled $s 2>/dev/null)"
done

line "端口监听（本机）"
ss -tlnp 2>/dev/null | awk 'NR==1 || /:8000|:5432|:443|:8443|:22 |:80 /' || true

line "应用"
if [ -x "$APP_DIR/venv/bin/python" ]; then
    echo "Python  : $("$APP_DIR/venv/bin/python" -V 2>&1)"
    echo "依赖包  : $("$APP_DIR/venv/bin/pip" list 2>/dev/null | wc -l) 个"
    echo "代码大小: $(du -sh "$APP_DIR" --exclude=venv 2>/dev/null | cut -f1)"
    echo "venv 大小: $(du -sh "$APP_DIR/venv" 2>/dev/null | cut -f1)"
fi
echo "版本    : $(curl -s --max-time 8 http://127.0.0.1:8000/api/version || echo '无响应')"
echo "调度器  : $(curl -s --max-time 8 http://127.0.0.1:8000/api/boot | grep -o '"scheduler_alive":[a-z]*' || echo '无响应')"

line "数据库"
if command -v sudo >/dev/null && sudo -n -u postgres true 2>/dev/null; then
    sudo -u postgres psql -tAc "SELECT '数据库大小: '||pg_size_pretty(pg_database_size('fund'))" 2>/dev/null
    sudo -u postgres psql -d fund -tAc "
        SELECT '  '||relname||': '||n_live_tup||' 行'
        FROM pg_stat_user_tables ORDER BY n_live_tup DESC;" 2>/dev/null
    sudo -u postgres psql -tAc "SELECT '连接数: '||count(*)||' / '||current_setting('max_connections') FROM pg_stat_activity" 2>/dev/null
fi

line "HTTPS 证书"
CERT=$(find /var/lib/caddy -name '*.crt' 2>/dev/null | head -1)
if [ -n "$CERT" ]; then
    echo "文件    : $CERT"
    openssl x509 -in "$CERT" -noout -subject -issuer -dates 2>/dev/null
else
    echo "还没拿到证书，看日志：journalctl -u caddy -n 30"
fi

line "资源占用 Top 5"
ps -eo pid,comm,%cpu,%mem --sort=-%mem 2>/dev/null | head -6

line "最近错误（若有）"
journalctl -u fund-monitor -p err -n 5 --no-pager 2>/dev/null || echo "无"
journalctl -u caddy -p err -n 5 --no-pager 2>/dev/null || true

echo
