# NodeSeek Monitor

一个部署在 Debian 12 / Docker 上的 NodeSeek 新帖关键词监控服务。

工作流程：

```
NodeSeek RSS (全站最新帖)
  → 定时拉取 (默认 30s)
  → 解析为统一 Post 对象
  → 关键词匹配 (大小写不敏感, 标题+摘要)
  → SQLite 去重 (容器重启不丢)
  → 命中且首次出现 → Telegram Bot 推送
  → 记录已推送 (幂等, 失败自动重试)
```

> 设计要点：NodeSeek 没有提供按关键词过滤的 RSS，因此程序拉取**全站最新 RSS**，
> 在内部做关键词匹配。RSS 只返回最近约 20 条，所以监控的是「最近更新」，而非全站历史搜索。

---

## 一、配置（全部通过环境变量）

复制示例并填入真实值：

```bash
cp .env.example .env
```

`.env.example` 是入库的配置模板（`.gitignore` 里的 `.env.*` 规则对它做了例外），
真实的 `.env` 与 `.env.local` 等一律不会进仓库。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RSS_URL` | `https://rss.nodeseek.com/` | NodeSeek RSS 地址 |
| `POLL_INTERVAL_SECONDS` | `30` | 轮询间隔（秒），可改 `10` / `60` |
| `KEYWORDS` | `CloudCone` | 监控关键词，逗号分隔，如 `CloudCone,DMIT,搬瓦工` |
| `TELEGRAM_BOT_TOKEN` | 空 | 从 @BotFather 获取，**仅放 `.env`** |
| `TELEGRAM_CHAT_ID` | 空 | 接收消息的 chat id |
| `DATABASE_PATH` | `/app/data/nodeseek.db` | SQLite 路径（容器内） |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

**改关键词**：编辑 `.env` 的 `KEYWORDS`，然后 `docker compose up -d` 重启即可。

---

## 二、本地运行（不依赖 Docker，用于调试）

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp .env.example .env   # 填入 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
python -m app.main
```

未配置 Telegram 时，程序以 **monitor-only** 模式运行（只记录、不推送），便于先验证解析与匹配。

离线自测（无需网络 / 真实 Telegram）：

```bash
python scripts/smoke_test.py
```

覆盖：RSS 解析、无时区 pubDate 按 UTC 解释、关键词匹配、首次静默、基线门控、
幂等去重、Telegram 失败重试、关键词变更后同步、日志不重复、Token 不泄露、消息格式。

容器内同样可以跑（镜像已包含 `scripts/`，需先按第一节创建 `.env`，因为 compose 会读取它）：

```bash
docker compose run --rm nodeseek-monitor python scripts/smoke_test.py
```

---

## 三、Debian 12 部署（Docker）

### 1. 安装 Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
```

### 2. 部署本项目

```bash
git clone https://github.com/DevQQQQQ/nodeSeekMonitor.git && cd nodeSeekMonitor
cp .env.example .env
nano .env          # 填写 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，按需修改 KEYWORDS

格式如下
RSS_URL=https://rss.nodeseek.com/
POLL_INTERVAL_SECONDS=30
KEYWORDS=关键词1,关键词2
TELEGRAM_BOT_TOKEN=填写你的TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=填写你的TELEGRAM_CHAT_ID
DATABASE_PATH=/app/data/nodeseek.db
LOG_LEVEL=INFO

docker compose up -d
docker compose logs -f
```

看到如下日志即正常运行：

```
2026-08-21 09:25:39 [INFO] First-run baseline established, posts=20 (no notifications sent)
2026-08-21 09:25:39 [INFO] Monitoring keywords=CloudCone interval=30s
2026-08-21 09:25:40 [INFO] RSS fetch success, items=20
2026-08-21 09:25:40 [INFO] No new matching posts
```

### 3. 日常运维

```bash
docker compose up -d     # 修改 .env 后重启生效（如换关键词）
docker compose logs -f   # 实时日志
docker compose down      # 停止
```

`./data` 已挂载到宿主机，容器重建 / 重启后 SQLite 数据不丢。
容器日志走 Docker 的 json-file 驱动，已限制为单文件 10MB × 3 个，不会无限增长；
查看日志请用 `docker compose logs`。

---

## 四、Telegram 消息格式

```
🚨 NodeSeek 关键词提醒

关键词：CloudCone

标题：CloudCone 新套餐补货

作者：alice
分类：trade
时间：2026-08-21 08:30:00
链接：https://www.nodeseek.com/post-xxxx
```

- 命中的关键词会列出（多关键词命中时全部显示）。
- 时间显示为**北京时间 (UTC+8)**。
- 不含任何敏感信息。

---

## 五、首次启动行为（first-run silence）

程序第一次启动时会把当前 RSS 里的所有帖子写入 SQLite 作为「已见基线」，
**不会**向 Telegram 推送任何历史帖子。之后出现的新帖（且匹配关键词）才会推送。

基线是否建立成功会被持久记录在数据库的 `meta` 表里：

- **基线建立成功** → 之后重启不会再重新建立基线，直接进入轮询。
- **启动时 RSS 抓取失败** → 基线**不会**被标记为已建立，程序停留在「通知关闭」状态
  并每隔一个轮询间隔重试。这是刻意设计：若把抓取失败当成「空基线」，下一轮抓取成功后
  feed 里的帖子会被全部误判为新帖，向用户推送一整批历史帖。基线未就绪期间**不会有任何推送**。
- 从旧版本升级上来的数据库（`seen_posts` 里已有数据但没有 `meta` 表）会被自动识别并
  视为「基线已建立」，避免升级后重复推送。

> 本地调试注意：`.env` 里 `DATABASE_PATH` 默认是容器内路径 `/app/data/nodeseek.db`。
> 在 Windows / macOS 上直接 `python -m app.main` 时，程序会在当前盘符根目录下创建
> `app/data/`。本地调试建议在 `.env` 中改为相对路径，例如 `DATABASE_PATH=./data/nodeseek.db`。

---

## 六、可靠性与边界

- **去重 / 幂等**：以 RSS `<guid>`（稳定数字 ID）为主键；同一帖子只成功推送一次。
- **基线门控**：基线未建立前不进入通知路径（见第五节），启动时的网络抖动不会造成历史帖轰炸。
- **失败重试**：Telegram 发送失败不会标记「已通知」，下一轮轮询会再次尝试，不会永久丢失。
- **异常不退出**：HTTP 403/429/500、超时、DNS 失败、XML 异常、Telegram 错误都会被记录，
  等待下一轮继续运行；连续失败升级为 `ERROR` 日志。
- **日志精简**：已推送过的帖子不会在每轮轮询时重复打日志，只有真正发生推送时才记录命中。
- **密钥安全**：Token 只存在于 `.env`（已被 `.gitignore` 排除），不进代码 / Dockerfile / 日志。
  异常信息中的 Token 会被主动替换为 `***`，不依赖「恰好没打印到」。

---

## 七、已知限制 / 风险

1. RSS 仅返回最近约 20 条，极高发帖时段可能漏掉刚发布又很快被挤出的帖子；
   必要时缩短 `POLL_INTERVAL_SECONDS`（如 10）。
2. 关键词改为新词后，已存在基线中的旧帖若恰好命中，会在下一轮被补推一次
   （符合「每个帖子最多通知一次」语义）。
3. RSS 数据结构若变动，解析失败会记 `ERROR` 并跳过该轮；`parser.py` 与 Telegram 解耦，便于单独调整。
