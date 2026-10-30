# 阿里云部署执行记录

- 执行时间：2026-10-30 21:00 ~ 22:00（Asia/Shanghai）
- 目标：把基金涨跌监控从 Render + Neon 迁到阿里云 ECS 自建全栈（本机 PostgreSQL + Caddy + systemd）
- 当前结论：**服务已上线并稳定运行，数据迁移因 Neon 侧超配额暂未完成**

---

## 一、目标环境

| 项 | 值 |
|---|---|
| 公网 IP | `47.111.231.52` |
| 主机名 | `iZbp180iv3npsbnedj0f16Z` |
| 系统 | Ubuntu 24.04.5 LTS (x86_64) |
| 规格 | 2 vCPU / 1.7 GB RAM / 40 GB 系统盘（剩余 35 GB） |
| 应用目录 | `/opt/fund-monitor` |
| 运行用户 | `fund`（system 用户，nologin） |
| 数据库 | 本机 PostgreSQL 16，库名 `fund`，用户 `fund` |
| 数据库密码 | 存在服务器 `/root/.fund_monitor_db_pass`（600） |
| 反代 | Caddy 2.11.7，443（LE IP 证书）+ 8443（HTTP 保底） |
| 进程守护 | systemd `fund-monitor.service`（gunicorn 1 worker × 8 threads，绑 127.0.0.1:8000） |
| 应用版本 | v3.34（commit `db-degrade-on-network-fail`） |
| 访问地址 | `https://47.111.231.52`（主）/ `http://47.111.231.52:8443`（保底） |
| SSH 密钥 | 本机 `C:\Users\Zupeng Lin\.ssh\id_fund_aliyun`（ed25519，免密 root） |

---

## 二、本次改动清单

### 新增 / 修改的文件

| 文件 | 改动 |
|---|---|
| `deploy/setup.sh` | ① 支持 `setup.sh <公网IP>` 参数，带 IP 自动切 HTTPS 配置，不带 IP 退回 8443；② 新增 2 GB swap（小内存机防 pip 编译 OOM）；③ 自动创建 `/var/log/caddy` 并修正属主；④ `caddy validate` 失败时降级用 `caddy adapt` 检查；⑤ ufw 补放行 443；⑥ 末尾自动启动服务并自检；⑦ `.env` 补写 `VAPID_SUBJECT`、老 `.env` 缺 `TZ` 时自动补；⑧ 记录上次 IP 到 `/etc/fund-monitor.public-ip`，重跑不会丢 HTTPS |
| `deploy/Caddyfile.ip-https` | ① 全局 `default_sni`（关键，见踩坑 3）；② `tls { issuer acme { profile shortlived } }` 正确语法；③ IP 用 `__PUBLIC_IP__` 占位符自动替换；④ 保留 `:8443` 保底入口；⑤ `X-Real-IP` 改用 `{http.request.remote.host}`（原 `{remote_host}` 带端口） |
| `deploy/Caddyfile.http` | 同步修正 `X-Real-IP` 取值 |
| `deploy/update.sh` | **新增**：日常更新脚本（修属主 → 装依赖 → py_compile → 重启 → 自检） |
| `DEPLOY.md` | 顶部新增「阿里云 ECS 部署」章节，原 Render 方案降为「旧方案」 |

### 服务器侧实际状态

- apt 安装：`python3-venv/pip/dev`、`build-essential`、`libpq-dev`、`postgresql`(16)、`git`、`curl`、`rsync`、`ufw`、`caddy`(2.11.7，cloudsmith 源)
- PostgreSQL 调优：`shared_buffers=256MB`、`effective_cache_size=768MB`、`work_mem=8MB`、`maintenance_work_mem=64MB`、`max_connections=50`
- swap：`/swapfile` 2 GB，`vm.swappiness=10`，已写 `/etc/fstab`
- journald 上限 200 MB
- ufw：active，放行 `22`、`443`、`8443`（IPv4+IPv6）
- 8 张表已由应用自动建好：`funds`、`rules`、`cumulative_state`、`nav_history`、`alert_log`、`intraday_log`、`config`、`push_subscriptions`

---

## 三、执行步骤（实际走通的顺序）

```bash
# 1. 本机生成免密密钥（一次性）
ssh-keygen -t ed25519 -f "$HOME\.ssh\id_fund_aliyun" -N '""' -C "fund-deploy@local"
# 公钥已加到服务器 root 的 ~/.ssh/authorized_keys

# 2. 打包上传（排除 venv/data/.git/探测脚本）
tar -czf app.tar.gz --exclude=venv --exclude=data --exclude=.git \
    --exclude=.codebuddy --exclude=__pycache__ --exclude=_probe*
scp -i ~/.ssh/id_fund_aliyun app.tar.gz root@47.111.231.52:/tmp/
ssh root@47.111.231.52 "mkdir -p /opt/fund-monitor && tar -xzf /tmp/app.tar.gz -C /opt/fund-monitor"

# 3. 初始化（幂等，可重复跑）
ssh root@47.111.231.52 "bash /opt/fund-monitor/deploy/setup.sh 47.111.231.52"

# 4. 数据迁移（⚠️ 本次失败，见第四节）
ssh root@47.111.231.52 "cd /opt/fund-monitor && \
  sudo -u fund NEON_URL='postgresql://...' venv/bin/python deploy/migrate.py"
```

---

## 四、踩坑与解决

### 1. Caddy 报 `unknown subdirective: profile`

- 原因：Caddy 2.11 的短周期证书 profile 不在 `tls` 下，而在 ACME issuer 下。
- 错误写法：`tls { profile shortlived }`
- 正确写法：

```caddyfile
tls {
    issuer acme {
        profile shortlived
    }
}
```

### 2. Caddy 起不来：`open /var/log/caddy/fund-monitor.log: permission denied`

- 原因：`caddy validate` 以 **root** 跑时会顺手把日志文件建成 `root:root 0600`，之后服务以 `caddy` 用户运行就写不进去。
- 解决：校验后统一 `chown -R caddy:caddy /var/log/caddy`；脚本里已内置。排查时先 `rm -f /var/log/caddy/*.log` 再重启。

### 3. HTTPS 握手失败 `SEC_E_ILLEGAL_MESSAGE`（最隐蔽的一个）

- 现象：证书已成功签发（SAN=`IP Address:47.111.231.52`，LE YE2，6 天有效），但浏览器/curl 连 443 直接 TLS 握手失败；`curl -k` 也不通。
- 原因：**RFC 6066 不允许 SNI 使用 IP 字面量**，Windows Schannel 明确日志 `using IP address, SNI is not supported by OS`。Caddy 收不到 SNI 就无法选择 IP 证书，握手直接 fatal alert。
- 解决：Caddyfile 全局加 `default_sni <公网IP>`，让无 SNI 连接也能拿到 IP 证书。改完立即 200。

```caddyfile
{
    default_sni 47.111.231.52
}
```

### 4. Neon 超配额，数据迁移中断

```
ERROR: Your account or project has exceeded the quota. Upgrade your plan to increase limits.
```

- pooler（`-pooler` 域名）和直连（裸 `ep-xxx` 域名）**都**报同一个错 → 是账号/项目级限额，不是连接池问题。
- 老站点 `https://fund-watch.onrender.com/api/funds` 返回 `[]`，说明 Render 侧同样已经读不到数据（走了 DB 降级路径）。
- 因此 `deploy/migrate.py` 在 `psycopg2.connect(src_url)` 处退出，**目标库仍是空库**（`funds=0 rules=0 config=1`）。

### 5. 端口探测误判

- 初期 `Test-NetConnection 443/8443` 全 False，误以为阿里云安全组没放行。
- 实际是 **当时没有进程监听**，连接被拒。Caddy 起来后三个端口全 True，安全组并无问题。

### 6. 首次 `setup.sh` 中途退出

- 因为 `set -e` + caddy validate 失败，脚本停在 Caddy 阶段，后面的 ufw、启动服务都没执行。
- 处理：修好语法后重跑一次 `setup.sh 47.111.231.52`，验证了幂等性（用户/库/venv 不重建，`.env` 保留）。

---

## 五、当前验证结果

| 检查项 | 结果 |
|---|---|
| `systemctl is-active fund-monitor` | active |
| `systemctl is-active caddy` | active |
| `https://47.111.231.52/api/version` | 200 `{"commit":"db-degrade-on-network-fail","version":"3.34"}` |
| `http://47.111.231.52:8443/api/version` | 200 |
| `https://47.111.231.52/`（首页） | 200，17039 bytes |
| `/static/app.js` `/static/sw.js` `/static/manifest.json` | 均 200 |
| `/api/db_diag` | `{"ok":true,"result":{"ok":1}}` |
| `/api/boot` | `init_db_error: null`，`scheduler_alive: true` |
| 调度线程 | cycle 4，`last_cycle_ms=216`，无 error |
| TLS 证书 | LE `YE2` 签发，`2026-11-06` 到期，Caddy 自动续 |
| ufw | active，放行 22 / 443 / 8443 |

---

## 六、待办（需要你处理）

### 1. Neon 数据迁回（阻塞中）

先登录 https://console.neon.tech 看用量，处理方式二选一：

- **恢复额度**（升级计划 / 等下月重置 / 删历史表减存储）
- 确认能连上后执行迁移（幂等，重复跑会先清空目标表）：

```powershell
ssh -i "$HOME\.ssh\id_fund_aliyun" root@47.111.231.52 "cd /opt/fund-monitor && \
  sudo -u fund NEON_URL='postgresql://neondb_owner:...@ep-mute-queen-ay496wb4-pooler.c-5.us-east-2.aws.neon.tech/neondb?sslmode=require' \
  venv/bin/python deploy/migrate.py"
```

迁移会一并带过来：基金列表、规则、累计状态、净值历史、提醒记录、盘中数据、**通知配置（Server酱/PushPlus/邮箱）**、**推送订阅**、**VAPID 密钥**。
→ 因为 VAPID 密钥跟着 `config` 表走，**手机端推送订阅不用重新授权**。

### 2. 如果放弃迁移（Neon 不想续）

- 新站是空库，需要重新在页面里添加基金和规则、重填通知配置。
- VAPID 密钥会重新生成，**所有设备需要重新打开「浏览器推送」开关**。

### 3. Render 老站要停掉

两边同时跑会**重复扫描、重复推送**。迁移完成并验证后：

- Render Dashboard → 该 Web Service → Suspend
- cron-job.org 那个每 10 分钟的唤醒任务也要删掉

阿里云这边**不需要** cron：调度器在 gunicorn 进程内常驻（`/api/boot` 可看到 `scheduler_alive: true`），没有休眠问题。

### 4. 待观察项

- 内存只有 1.7 GB，已加 2 GB swap。若后续频繁 swap，考虑升到 2 vCPU / 4 GB。
- Caddy IP 证书 6 天有效期，靠后台自动续。若连续多天访问异常，先看 `journalctl -u caddy | grep -i obtain`。

---

## 七、日常运维

```bash
# 本机：上传新代码（改完 app.py 等）
ssh -i "~/.ssh/id_fund_aliyun" ...   # 或用下面的 tar+scp
tar -czf app.tar.gz --exclude=venv --exclude=data --exclude=.git --exclude=_probe* .
scp -i "~/.ssh/id_fund_aliyun" app.tar.gz root@47.111.231.52:/tmp/
ssh -i "~/.ssh/id_fund_aliyun" root@47.111.231.52 \
  "tar -xzf /tmp/app.tar.gz -C /opt/fund-monitor && bash /opt/fund-monitor/deploy/update.sh"
```

```bash
# 服务器
systemctl status fund-monitor        # 服务状态
journalctl -u fund-monitor -f        # 实时日志
systemctl restart fund-monitor       # 重启
journalctl -u caddy -n 50            # 证书/反代日志
tail -f /var/log/caddy/fund-monitor.log
curl -s localhost:8000/api/boot      # 调度器状态
```

```bash
# 备份数据库（建议加进 crontab，每天 3 点）
sudo -u postgres pg_dump fund | gzip > /root/fund-$(date +%F).sql.gz
```

---

## 八、安全提醒

- Neon 连接串含明文密码，且本次通过聊天传递过。迁移完成后建议在 Neon 控制台**轮换 `neondb_owner` 密码**，或直接删掉该项目。
- 本机私钥 `C:\Users\Zupeng Lin\.ssh\id_fund_aliyun` 无口令短语，注意不要同步到云盘/仓库。
- PostgreSQL 只监听本机，应用只绑 `127.0.0.1:8000`，外网仅 Caddy 的 443/8443 可达，符合最小暴露面。
