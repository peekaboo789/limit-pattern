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
# 可选：用环境变量固定登录账号（不设则第一次打开网页时创建管理员账号）
# printf 'LP_USER=admin\nLP_PASSWORD=换成你自己的密码\n' > .env
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
      LP_USER: ${LP_USER:-}
      LP_PASSWORD: ${LP_PASSWORD:-}
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

登录（v0.5.1）：网页版必须登录。第一次打开显示「创建管理员账号」；或用 `LP_USER` / `LP_PASSWORD` 环境变量指定（v0.5.0 的 LP_PASSWORD 照常可用，用户名默认 admin；
v0.5.0 在设置里设过的访问密码会自动迁移为 admin 账号）。勾选「记住我」后 cookie 长期有效（HttpOnly、SameSite=Lax，HTTPS 下 Secure），
直到改用户名/密码或点「退出所有设备」。账号（加盐 PBKDF2 哈希）和签名密钥都在 `/data`（auth.json、.session_secret），重启和升级都不会掉线。
连续输错 5 次锁定该 IP 5 分钟。只有 `/api/health`、登录相关接口和 `/api/wecom/callback` 免登录。
暴露到公网时请在前面加 HTTPS 反向代理（Nginx / Caddy），企业微信回调也必须是 HTTPS。

## 升级

### 程序内一键更新（v0.5.3 起，推荐）

设置 → 在线更新 → 检查更新 → 下载 → 一键更新（二次确认）。**不需要挂载 docker.sock，也不用改 docker-compose.yml。**

1. 程序从 GitHub Release 下载 `limit-pattern-app-<版本>.tar.gz`（后端代码 + 网页）到 `/data/updates/`，校验 sha256；
2. 备份设置、数据库、自选股、账号到 `data/backups/`（保留最近 3 份）；
3. 程序退出并由容器里的监督进程（`/opt/lp/supervisor.py`，镜像入口）接管：解压到 `/data/app/<版本>`、核对依赖、导入自检、启动新版本并检查 `/api/health`；
4. 新版本起不来 → 自动回退到上一个版本或镜像自带版本，设置页会显示失败原因。

规则：
- 运行「镜像自带版本」和「程序内更新版本」中**较新**的一个；以后 `docker compose pull` 到更新的镜像会自动接管，旧的程序内更新自动清理。
- 新版本如果需要新的 Python 依赖（更新包里带 requirements 哈希），程序内更新会拒绝，并提示改用 `docker compose pull`。
- 监督进程自己负责重启程序，不依赖 Docker 的 restart 策略；compose 里保留 `restart: unless-stopped` 只是为了宿主机重启后自动起来。
- v0.5.2 及更早的镜像没有监督进程：先手动 `docker compose pull && docker compose up -d` 升到 v0.5.3，之后就能程序内更新。

### 用新镜像升级

```bash
docker compose pull && docker compose up -d
```

想全自动拉镜像，可以用 Watchtower（它需要挂 docker.sock，权限隔离在它自己的容器里；本程序不需要）：

```yaml
  watchtower:
    image: containrrr/watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --cleanup --schedule "0 0 20 * * *" limit-pattern
```

新版本第一次启动会自动把设置、数据库、自选股、账号备份到 `data/backups/`（保留最近 3 份），再做数据迁移。
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
