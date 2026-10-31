# 新浪全球指数转发代理

给阿里云服务器用的取数跳板 —— **只做一件事：把新浪的 36 个全球指数转成 JSON**。

## 为什么需要它

阿里云 ECS 的机房 IP 被新浪 `hq.sinajs.cn` 全线 403（连 `sh000001` 都拿不到），
同一份代码在家用宽带上 200 正常 —— 是 IP 层封锁，不是接口废弃。
这个服务跑在 Render（美国节点），取到数据后由阿里云回来拿。

期间试过但不可行的通道：Yahoo（按 IP 返回中文 403 拦截页）、
`*.workers.dev`（DNS 污染到 Facebook 的 IP）、`*.vercel.app`（被墙）、
台湾证交所（被墙）、东财 push2（访问几次就封整个 IP 的行情域名 40 分钟以上）。

## 部署（Render，约 3 分钟）

1. Render → **New → Web Service** → 选 `Penn-Lin/Fund-Watch` 仓库
2. **Root Directory** 填 `proxy` ← 关键，否则会去装根目录那一堆依赖
3. Runtime 选 **Python**，Build Command `pip install -r requirements.txt`
4. Start Command：
   ```
   gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
   ```
5. **环境变量一个都不用配**（没有数据库、没有密钥、没有调度器）
6. 部署完访问 `https://<服务名>.onrender.com/health`，返回
   `{"ok": true, "cached": 36, ...}` 就成功了

## 额度说明

- **不碰任何数据库** → Neon 配额消耗恒为 0（代码里连数据库驱动都没装）
- 免费实例 15 分钟无请求休眠、休眠期间**不消耗实例小时**
- 阿里云只在亚太交易时段（08:00-14:30）每 5 分钟调一次 → 约 143 h/月 = **19%**
- 冷启动 5~8 秒（比复用整套基金监控的 32 秒快得多），后端定时抓取无感

## 接口

| 路径 | 说明 |
|---|---|
| `/` 或 `/api/sina_global` | 返回 36 个市场的 JSON 数组（20 秒进程内缓存）|
| `/health` | 健康检查，带缓存条数和年龄 |

字段：`code` `name` `price` `change` `change_pct` `quote_date` `quote_time`
`open` `prev_close` `market`（`apac` / `eu` / `am` / `me`）

## 本地跑

```bash
pip install -r requirements.txt
python app.py          # 默认 5099 端口
```
