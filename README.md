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

覆盖：RSS 解析、关键词匹配、首次静默、幂等去重、Telegram 失败重试、消息格式。

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
git clone https://github.com/DevQQQQQ/nodeseek-monitor.git && cd nodeseek-monitor
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

`./data` 与 `./logs` 已挂载到宿主机，容器重建 / 重启后 SQLite 数据不丢。

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

---

## 六、可靠性与边界

- **去重 / 幂等**：以 RSS `<guid>`（稳定数字 ID）为主键；同一帖子只成功推送一次。
- **失败重试**：Telegram 发送失败不会标记「已通知」，下一轮轮询会再次尝试，不会永久丢失。
- **异常不退出**：HTTP 403/429/500、超时、DNS 失败、XML 异常、Telegram 错误都会被记录，
  等待下一轮继续运行；连续失败升级为 `ERROR` 日志。
- **密钥安全**：Token 只存在于 `.env`（已被 `.gitignore` 排除），不进代码 / Dockerfile / 日志。

---

## 七、已知限制 / 风险

1. RSS 仅返回最近约 20 条，极高发帖时段可能漏掉刚发布又很快被挤出的帖子；
   必要时缩短 `POLL_INTERVAL_SECONDS`（如 10）。
2. 关键词改为新词后，已存在基线中的旧帖若恰好命中，会在下一轮被补推一次
   （符合「每个帖子最多通知一次」语义）。
3. RSS 数据结构若变动，解析失败会记 `ERROR` 并跳过该轮；`parser.py` 与 Telegram 解耦，便于单独调整。
