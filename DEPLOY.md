# 免费云端部署指南（Render + Neon + cron-job.org）

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

## 注意事项

- 东财接口从海外服务器访问通常没问题，如遇风控可在 Neon 同区域多试几次。
- Neon 免费层 0.5GB 存储，本应用每次扫描写入量极小，且代码自动清理 90 天前数据，够用。
- 免费实例冷启动首次打开约需 30~60 秒，属正常现象。
