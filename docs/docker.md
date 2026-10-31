# Docker 部署（网页版）

镜像：`ghcr.io/peekaboo789/limit-pattern`（linux/amd64 + linux/arm64），标签 `latest`、版本号（如 `0.5.0`）、`sha-xxxxxxx`。

容器里没有桌面窗口：FastAPI 在 8000 端口同时提供接口和网页，用浏览器打开 `http://服务器IP:8000`。
定时任务都在容器里跑：17:10 日线同步（之后自动写每日总结、预热回测缓存）、17:40 清理、18:10 补写总结、11:30 冻结快照、14:50/15:10 AI 榜单，
以及交易时段每 15 秒一次的盘中监控（AI 榜单 + 自选股 ≥3%/≥6% 报警，可推企业微信）。
页面开着时用浏览器通知（设置 → 企业微信 → 允许浏览器通知）；导出文件直接由浏览器下载。

## 配置要求

| 项目 | 最低 |
| --- | --- |
| CPU | 2 核 |
| 内存 | 2 GB |
| 磁盘 | 2 GB（数据库随保留天数增长，60 个交易日约几百 MB） |
| 网络 | **中国大陆网络**：东方财富 / 腾讯 / 新浪行情源在境外经常不通或被限速 |
| 软件 | Docker 20.10+ 与 Docker Compose v2 |

## 安装

```bash
mkdir limit-pattern && cd limit-pattern
curl -O https://raw.githubusercontent.com/peekaboo789/limit-pattern/cursor/limit-pattern-system-8abb/docker-compose.yml
# 可选：网页访问密码，写进同目录 .env（不要提交到 git）
echo 'LP_PASSWORD=换成你自己的密码' > .env
docker compose up -d
docker compose logs -f   # 首次启动库是空的，会自动拉一次日线
```

`docker-compose.yml`：

```yaml
services:
  limit-pattern:
    image: ghcr.io/peekaboo789/limit-pattern:latest
    container_name: limit-pattern
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    environment:
      TZ: Asia/Shanghai
      LP_PASSWORD: ${LP_PASSWORD:-}
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

访问密码：设置 `LP_PASSWORD` 环境变量，或在 设置 → 网页访问密码 里设（只存 PBKDF2 哈希）。设置后除 `/api/health` 和 `/api/wecom/callback` 外的接口都要先登录（HttpOnly 会话 cookie，30 天；改密码后旧会话失效）。
暴露到公网时请在前面加 HTTPS 反向代理（Nginx / Caddy），企业微信回调也必须是 HTTPS。

## 升级

```bash
docker compose pull && docker compose up -d
```

数据都在 `./data`（SQLite 数据库、settings.json、导出、总结、自选股），升级不会丢。

## 备份 / 恢复

```bash
docker compose stop
tar czf limit-pattern-backup-$(date +%F).tgz data
docker compose start
```

恢复：停容器，解压覆盖 `data/`，再启动。`data/settings.json` 里有企业微信 Secret 等密钥，备份文件请妥善保管。

## 自己构建

```bash
docker build -t limit-pattern:local .
docker run -d -p 8000:8000 -v $PWD/data:/data limit-pattern:local
```

## 企业微信

见 [wecom.md](wecom.md)。
