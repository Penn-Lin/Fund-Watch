#!/usr/bin/env bash
# 基金涨跌监控 —— 服务器端更新脚本（代码已上传到 /opt/fund-monitor 后执行）
# 用法：sudo bash /opt/fund-monitor/deploy/update.sh
# 做的事：修属主 → 补依赖 → 重启 → 自检；不动数据库和 .env
set -euo pipefail

APP_DIR=/opt/fund-monitor
APP_USER=fund

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行" >&2; exit 1; }

log "修正文件属主"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# rsync 上传会带上本地权限，模板/静态文件必须可读
find "$APP_DIR" -path "$APP_DIR/venv" -prune -o -type f -exec chmod 644 {} +
find "$APP_DIR" -path "$APP_DIR/venv" -prune -o -type d -exec chmod 755 {} +
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

log "更新依赖"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

log "语法检查"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -m py_compile \
    "$APP_DIR/app.py" "$APP_DIR/database.py" "$APP_DIR/fund_data.py" \
    "$APP_DIR/rule_engine.py" "$APP_DIR/notifier.py" "$APP_DIR/vapid.py"

log "重启服务"
systemctl restart fund-monitor
sleep 3
systemctl is-active --quiet fund-monitor || {
    journalctl -u fund-monitor -n 40 --no-pager
    exit 1
}

log "自检"
curl -fsS --max-time 15 http://127.0.0.1:8000/api/version || echo '本机 /api/version 无响应'
echo
curl -fsS --max-time 15 http://127.0.0.1:8000/api/db_diag || echo '/api/db_diag 无响应（数据库可能没通）'
