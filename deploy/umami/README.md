# Umami 试点部署

Ubuntu 24.04 安装 `docker.io`、`docker-compose-v2`、`curl` 和 `openssl`，启用 Docker。以下命令在专用试点服务器上由管理员执行。将本目录的 `compose.yaml`、`pilot-probe.sh` 和两个 systemd 单元放到 `/opt/ops-recovery-pilot`，目录权限设为 `0700`。

```bash
cd /opt/ops-recovery-pilot
umask 077
if [ ! -e .env ]; then
  printf 'POSTGRES_PASSWORD=%s\nAPP_SECRET=%s\nTWO_FACTOR_ENCRYPTION_KEY=%s\n' \
    "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env
fi
```

首次部署时拉取指定镜像，记录实际摘要。Docker Hub 连接超时时，PostgreSQL 可使用 AWS Public ECR 的 Docker Official Images 副本 `public.ecr.aws/docker/library/postgres:15-alpine`，替换下方同一引用即可。已有 `images.env` 时保留其内容，升级时单独审阅版本和摘要。

```bash
umami_image=ghcr.io/umami-software/umami:3.5.0
postgres_image=docker.io/library/postgres:15-alpine
if [ ! -e images.env ]; then
  docker pull "$umami_image" && docker pull "$postgres_image" &&
    printf 'UMAMI_IMAGE=%s\nPOSTGRES_IMAGE=%s\n' \
      "$(docker image inspect --format '{{index .RepoDigests 0}}' "$umami_image")" \
      "$(docker image inspect --format '{{index .RepoDigests 0}}' "$postgres_image")" > images.env
fi
docker compose --env-file .env --env-file images.env up -d --wait --wait-timeout 180
docker compose --env-file .env --env-file images.env ps
bash pilot-probe.sh --init
```

Umami HTTP 监听 `127.0.0.1:3000`。PostgreSQL 仅在 Compose 网络中使用，数据存入具名卷。首次登录 Umami 时按上游提示初始化管理员密码。可经 SSH 本地端口转发访问界面。

`--init` 为专用测试库注册固定 ID 的合成站点。探针向真正的 `/api/send` 写入唯一事件，再在 PostgreSQL 的只读事务中确认对应记录存在。成功记录包含探针耗时和事件路径，不包含密钥。探针测量收集链路与持久化，应用统计 API 的验证另行开展。

```bash
install -m 0644 ops-pilot-probe.service ops-pilot-probe.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ops-pilot-probe.timer
systemctl list-timers ops-pilot-probe.timer
journalctl -u ops-pilot-probe.service -n 20 --no-pager
```

定时器每分钟写入一个合成事件。可用 `systemctl disable --now ops-pilot-probe.timer` 暂停。探针的 Docker 权限留在受控主机服务，提供给 Agent 的适配器应只读取受限遥测。

从完整项目源代码目录安装会话 worker、观察器和动作代理。安装程序为 release 创建独立 Python 虚拟环境，使用只读观测和本机离线模型模式启动。

```bash
sudo bash deploy/umami/install-runtime.sh "$PWD" pilot-v2
sudo -u ops-recovery env OPS_STATE_DIR=/var/lib/ops-recovery-agent \
  /opt/ops-recovery-agent/runtime/bin/ops session new 'Check pilot health'
sudo -u ops-recovery env OPS_STATE_DIR=/var/lib/ops-recovery-agent \
  /opt/ops-recovery-agent/runtime/bin/ops session list
```

连续三个已完成探针失败会创建会话，连续两个成功会关闭同一告警。`ops session pilot-report --since <ISO-8601 时间>` 输出采样覆盖和告警记录。在状态目录中运行报告指令，确保非特权账号可以写入报告输出目录。

在业务压力窗口从服务器本机逐级运行 `python3 load.py --rate 1 --seconds 60 --concurrency 4`，按资源情况提高 `--rate`。报告保留计划请求数、实际请求数、应用接受数、因并发上限未发出的请求和 p50/p95/p99。用每分钟业务探针验证同期的数据回读，日志与会话并发评测一并留存。
