#!/usr/bin/env bash
# 本地运行：把当前 HEAD 推到云服务器并重启服务
#
# 用法：
#   bash deploy/push.sh              # 完整体检 + 上传 + 重启 + 验证
#   bash deploy/push.sh --fast       # 跳过本地自测
#   bash deploy/push.sh --dry        # 只看会改哪些文件，不上传
#
# 前提：改动必须先 git commit —— git archive 只打包已提交的内容，
#       这样线上跑的一定是某个确定的版本，出问题能精确回滚。
set -euo pipefail

HOST="root@47.111.231.52"
APP_DIR="/opt/fund-monitor"
FAST=0
DRY=0
for a in "$@"; do
    case "$a" in
        --fast) FAST=1 ;;
        --dry)  DRY=1 ;;
        *) echo "未知参数: $a"; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."

# ── 找密钥：$HOME 可能被别的软件改过，按顺序找 ──────────────
KEY=""
for p in "$HOME/.ssh/id_fund_aliyun" \
         "/c/Users/Zupeng Lin/.ssh/id_fund_aliyun" \
         "$USERPROFILE/.ssh/id_fund_aliyun"; do
    if [ -f "$p" ]; then KEY="$p"; break; fi
done
[ -n "$KEY" ] || { echo "找不到 SSH 密钥 id_fund_aliyun"; exit 1; }

# ── 检查工作区 ────────────────────────────────────────────
if [ -n "$(git status --porcelain)" ]; then
    echo "⚠️  有未提交的改动，这些不会被上传："
    git status --short
    echo
    echo "先提交再推送："
    echo "  git add -A && git commit -m \"说明\""
    exit 1
fi

echo "==> 待部署版本：$(git log --oneline -1)"
if [ "$DRY" = "1" ]; then
    echo "==> 相对线上可能变更的文件："
    git diff --stat HEAD~1 HEAD 2>/dev/null || true
    exit 0
fi

# ── 本地自测 ──────────────────────────────────────────────
if [ "$FAST" = "0" ]; then
    echo "==> 本地自测"
    ./venv/Scripts/python.exe selftest.py 2>&1 | tail -3
fi

# ── 上传 + 远端更新 ───────────────────────────────────────
# 用管道而不是先落盘，省掉临时文件，也避免误传旧的包
echo "==> 上传并重启"
git archive --format=tar.gz HEAD \
    | ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=no "$HOST" \
      "tar xzf - -C $APP_DIR && bash $APP_DIR/deploy/update.sh"

# ── 线上验证 ──────────────────────────────────────────────
echo "==> 线上版本"
curl -s --max-time 15 "http://47.111.231.52:8443/api/version" || echo '（无响应，去服务器看 journalctl -u fund-monitor -n 50）'
echo
