# 阿里云 ECS 部署（当前方案）

Ubuntu 24.04 + 本机 PostgreSQL 16 + Caddy（自动 HTTPS）+ systemd，全部脚本在 `deploy/`。
相比 Render：无冷启动、数据库同机无跨境延迟、可常驻定时扫描。

## 一次性初始化（在本机 PowerShell 执行）

```powershell
# 0) 免密登录（已生成 d:/... 之外的本机密钥）
ssh-keygen -t ed25519 -f "$HOME\.ssh\id_fund_aliyun"   # 已有则跳过
# 把 $HOME\.ssh\id_fund_aliyun.pub 内容加到服务器 root 的 ~/.ssh/authorized_keys
# （阿里云控制台 → 远程连接/Workbench 里粘贴，或实例创建时选密钥对）

# 1) 上传代码（排除本地虚拟环境与数据）
$IP = "<公网IP>"
scp -i "$HOME\.ssh\id_fund_aliyun" -r .\* root@${IP}:/opt/fund-monitor/
# 或用 rsync（若本机装了 Git Bash / WSL）
rsync -avz -e "ssh -i $HOME/.ssh/id_fund_aliyun" --exclude venv --exclude data --exclude .git ./ root@${IP}:/opt/fund-monitor/

# 2) 初始化（装包、建库、venv、systemd、Caddy HTTPS）
ssh -i "$HOME\.ssh\id_fund_aliyun" root@${IP} "bash /opt/fund-monitor/deploy/setup.sh ${IP}"

# 3) 迁移 Neon 数据（可选）
ssh -i "$HOME\.ssh\id_fund_aliyun" root@${IP} "cd /opt/fund-monitor && sudo -u fund NEON_URL='<Neon连接串>' venv/bin/python deploy/migrate.py"
```

阿里云安全组入方向需放行：`22`、`443`（IP 证书验证用）、`8443`（保底 HTTP 入口）。

## 日常更新

```powershell
rsync -avz -e "ssh -i $HOME/.ssh/id_fund_aliyun" --exclude venv --exclude data --exclude .env ./ root@${IP}:/opt/fund-monitor/
ssh -i "$HOME\.ssh\id_fund_aliyun" root@${IP} "bash /opt/fund-monitor/deploy/update.sh"
```

## 常用运维

```bash
systemctl status fund-monitor        # 服务状态
journalctl -u fund-monitor -f        # 实时日志
systemctl restart fund-monitor       # 重启
tail -f /var/log/caddy/fund-monitor.log  # 反代访问日志
```

## HTTPS 说明

- `setup.sh <公网IP>` 会写 `/etc/caddy/Caddyfile`：443 + LE 短周期 IP 证书（6 天，自动续），
  同时保留 `:8443` HTTP 保底入口。证书签不下来时仍能进 8443。
- 证书状态：`journalctl -u caddy -n 50` / `ls /var/lib/caddy/.local/share/caddy/certificates`
- 别删 `/var/lib/caddy`，删掉会重申请并触发 LE 限速。
- 纯 HTTP（8443）下 Service Worker / Web Push 不可用，系统级推送必须走 443 HTTPS。

---

# 旧方案：免费云端部署指南（Render + Neon + cron-job.org）

三件套全免费，部署后任何设备浏览器可访问，提醒 24 小时照常推送。

> 原理：Render 跑 Flask 网页服务；Neon 存数据（Render 免费实例重启会清空磁盘，
> 所以基金列表、规则、监控记录、通知配置全部存 Neon）；cron-job.org 每 10 分钟
> 访问一次 /api/refresh，既防止免费实例休眠，又保证定时扫描和提醒。

## 第 1 步：建免费数据库（Neon，2 分钟）

1. 打开 https://neon.tech → 注册（可用 GitHub 账号登录）
2. 创建项目（region 随便选，默认即可）
3. 复制连接串（Connection string，形如
   `postgresql://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require`）

## 第 2 步：推送代码到 GitHub

1. 打开 https://github.com/new 建一个**私有**仓库（如 fund-monitor）
2. 在本项目目录执行（首次需 GitHub 登录）：

```bash
git init
git add .
git commit -m "fund monitor"
git remote add origin https://github.com/<你的用户名>/fund-monitor.git
git push -u origin main
```

> config.json、data/、venv/ 已被 .gitignore 排除，密钥不会上传。

## 第 3 步：部署到 Render（3 分钟）

1. 打开 https://render.com → 注册（可用 GitHub 登录）
2. Dashboard → New → Web Service → 连接刚推的仓库
3. 关键配置（使用仓库里的 render.yaml 也可自动带出）：
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT app:app`
   - Instance Type: **Free**
   - 环境变量：
     - `DATABASE_URL` = 第 1 步复制的 Neon 连接串
     - `TZ` = `Asia/Shanghai`（必须，否则交易日/盘中判断会错）
     - `PYTHON_VERSION` = `3.11.9`
4. Create Web Service → 等待构建完成，得到公网地址
   `https://fund-monitor-xxxx.onrender.com`，手机浏览器直接访问

## 第 4 步：防休眠 + 定时扫描（cron-job.org，2 分钟）

Render 免费实例 15 分钟无访问会休眠，用免费 cron 唤醒：

1. 打开 https://cron-job.org → 注册
2. Create Cron Job：
   - URL: `https://你的地址.onrender.com/api/refresh`
   - Method: **POST**
   - Schedule: Every 10 minutes
   - 勾选异步失败通知（可选）
3. 保存即生效

盘中想更密集监控的话，把间隔调成 5 分钟即可（东财接口内部有 5 秒节流保护）。

## 部署后首次配置

打开公网页面 → 「规则设置」配置阈值 → 「通知设置」填 Server酱/PushPlus/邮箱
→ 点「发送测试提醒」验证。配置存在 Neon，实例重启不丢。

## Web Push 浏览器推送（推荐）

开启后基金触发预警时浏览器弹**系统级通知**，网页没开也能收到——这是接近原生 App 的推送体验。
本地开发无需任何配置：首次访问自动生成 VAPID 密钥并存入数据库。

### 生产环境固定密钥（推荐）

Render 免费实例磁盘不持久，但自动生成的密钥存在 DB config（Neon）里，跨重启仍稳定。
若希望多实例统一或便于迁移，用环境变量固定：

1. 在本地 venv 生成密钥对：

   ```bash
   python -c "import vapid; p,k=vapid._generate_keys(); print('PRIVATE:'); print(p); print('PUBLIC:'); print(k)"
   ```

2. Render → 你的 Web Service → Environment → 添加：
   - `VAPID_PRIVATE_KEY` = PRIVATE 输出（PEM，含 BEGIN/END 行）
   - `VAPID_PUBLIC_KEY` = PUBLIC 输出（base64url 或 PEM 均可）
   - `VAPID_SUBJECT` = `mailto:你的邮箱` 或 `https://你的域名.onrender.com`

> 不配环境变量也能用——代码会在 Neon 里自动生成并复用一份。

### 订阅推送

手机/电脑浏览器打开公网地址 → 「通知」页 → 「浏览器推送」开关打开 → 允许通知权限。
可多设备同时订阅，每台设备独立登记。把页面「添加到主屏幕/桌面」后即为 PWA，有图标可离线启动。

## 注意事项

- 东财接口从海外服务器访问通常没问题，如遇风控可在 Neon 同区域多试几次。
- Neon 免费层 0.5GB 存储，本应用每次扫描写入量极小，且代码自动清理 90 天前数据，够用。
- 免费实例冷启动首次打开约需 30~60 秒，属正常现象。
