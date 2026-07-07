# TokenMeter

TokenMeter 是一个可以自部署的 Token 用量统计工具，用来把多台服务器、多个 Agent、多个 profile 的使用记录汇总到一个 Web 页面里查看。

它目前主要面向本机和服务器上的 Agent 工作流：

- Hermes：读取 `~/.hermes/state.db` 以及 `~/.hermes/profiles/*/state.db`。
- OpenClaw：读取 `~/.openclaw/agents/*/sessions/*.trajectory.jsonl`。
- Codex：通过 `~/.codex/state_5.sqlite` 定位 rollout，并读取 `token_count.last_token_usage` 增量。
- ZCode：读取 `~/.zcode/cli/db/db.sqlite` 的 `model_usage`。
- WorkBuddy / Claude Code：读取各自 `projects/**/*.jsonl` 里的 usage 元数据。
- 多机器汇总：每台机器本地采集后上传到中心服务。
- Web 看板：按 Agent、时间范围、模型、历史日期查看 token 用量。

TokenMeter 只读取 Token 统计相关字段，不读取 Hermes 消息正文、Claude Code / WorkBuddy 消息正文或 prompt 内容。

## 功能特性

- 支持按 Agent 统计：OpenClaw、Hermes、Codex、ZCode、WorkBuddy、Claude Code。
- 支持区分 Hermes / OpenClaw 的 profile，并尽量从 cwd 或 Agent 配置推导其他工具的 profile。
- 支持按日期、模型、Agent 聚合。
- 支持中心 SQLite 存储，方便单机部署和备份。
- 支持 Web 页面查看今日、昨日、近 3 天、近 7 天、近 30 天。
- 支持真实 SVG 占比图，鼠标悬停可查看具体 token 和占比。
- 支持 favicon、Apple Touch Icon 和 Web App Manifest，保存到桌面时会显示专用图标。
- 支持简单 Bearer Token 保护 API。

## 快速开始

先在当前机器采集最近 24 小时的数据：

```sh
PYTHONPATH=src python3 -m tokenmeter collect --since 24h
```

导入本机数据到中心 SQLite：

```sh
PYTHONPATH=src python3 -m tokenmeter import \
  --db data/tokenmeter.sqlite \
  --since 24h
```

启动 Web 看板：

```sh
PYTHONPATH=src python3 -m tokenmeter serve \
  --bind 127.0.0.1:18888 \
  --db data/tokenmeter.sqlite
```

`serve` 默认会立即采集一次本机数据，之后每 15 分钟自动采集最近 1 天的数据并写入中心库。页面会每 60 秒自动刷新 API 数据。

如需关闭服务内自动采集：

```sh
PYTHONPATH=src python3 -m tokenmeter serve \
  --bind 127.0.0.1:18888 \
  --db data/tokenmeter.sqlite \
  --auto-import-interval 0
```

然后打开：

```text
http://127.0.0.1:18888/
```

如果要让同一局域网的其他机器访问，可以绑定到 `0.0.0.0`：

```sh
PYTHONPATH=src python3 -m tokenmeter serve \
  --bind 0.0.0.0:18888 \
  --db data/tokenmeter.sqlite
```

访问地址类似：

```text
http://<服务器 IP>:18888/
```

## 多服务器上传

中心服务器启动时建议配置访问 token：

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter serve \
  --bind 0.0.0.0:18888 \
  --db /var/lib/tokenmeter/tokenmeter.sqlite
```

中心服务器也可以调整自动采集间隔，例如每 15 分钟：

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter serve \
  --bind 0.0.0.0:18888 \
  --db /var/lib/tokenmeter/tokenmeter.sqlite \
  --auto-import-interval 15m \
  --auto-import-since 1d
```

其他服务器上传数据：

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter upload \
  --server http://<中心服务器 IP>:18888 \
  --host "$(hostname)" \
  --since 7d
```

建议后续用 systemd timer、cron 或 macOS LaunchAgent 定时执行 `tokenmeter upload`。

## 一键安装上传器

其他机器只需要执行下面这条命令，就会安装本机上传器、立即上传最近 30 天数据，并创建每 15 分钟上传最近 1 天数据的定时任务：

```sh
curl -fsSL http://your-tokenmeter.example.com/tokenmeter/install.sh | sh
```

如果目标机器需要 root 权限安装 systemd timer，可以使用：

```sh
curl -fsSL http://your-tokenmeter.example.com/tokenmeter/install.sh | sudo sh
```

默认中心地址是 `http://your-tokenmeter.example.com`。也可以显式指定：

```sh
curl -fsSL http://your-tokenmeter.example.com/tokenmeter/install.sh | sh -s -- "http://your-tokenmeter.example.com"
```

常用环境变量：

- `TOKENMETER_HOST`: 覆盖上报主机名，默认 `hostname`。
- `TOKENMETER_INTERVAL`: 定时上传间隔秒数，默认 `900`。
- `TOKENMETER_BOOTSTRAP_SINCE`: 首次上传窗口，默认 `30d`。
- `TOKENMETER_SINCE`: 后续定时上传窗口，默认 `1d`。
- `TOKENMETER_AGENTS`: 采集 Agent 列表，默认 `hermes,openclaw,codex,zcode,workbuddy,claude`。
- `TOKENMETER_TOKEN`: 如果中心服务启用了 Bearer Token，在这里填写。

## 常用命令

查看本机统计：

```sh
PYTHONPATH=src python3 -m tokenmeter collect --since 7d
```

按 JSON 输出：

```sh
PYTHONPATH=src python3 -m tokenmeter collect --since 7d --format json
```

查看中心库汇总：

```sh
PYTHONPATH=src python3 -m tokenmeter summary \
  --db data/tokenmeter.sqlite \
  --since 7d
```

按指定维度汇总：

```sh
PYTHONPATH=src python3 -m tokenmeter summary \
  --db data/tokenmeter.sqlite \
  --since 7d \
  --group-by host,agent,profile,model
```

调用 API：

```sh
curl -H "Authorization: Bearer $TOKENMETER_TOKEN" \
  "http://127.0.0.1:18888/api/v1/summary?since=7d&group_by=host,agent,profile,model"
```

## Web 页面

Web 页面包含：

- 顶部筛选：按 Agent 和时间范围筛选。
- 今日/昨日/区间数据：展示当前筛选范围的总 token、成本、活跃天数。
- 用量占比：真实 SVG 图表，悬停显示具体 token 和占比。
- 按模型：展示当前筛选范围内各模型占比。
- 按服务器：展示当前筛选范围内各上报服务器占比。
- 按 Profile：展示 OpenClaw 和 Hermes 的 profile 占比。
- 历史明细：展示完整历史日期数据，不随时间范围筛选收缩。

## 数据来源

Hermes 数据来源：

- `~/.hermes/state.db`
- `~/.hermes/profiles/<profile>/state.db`

OpenClaw 数据来源：

- `~/.openclaw/agents/<profile>/sessions/*.trajectory.jsonl`

OpenClaw 目前只统计 `model.completed` 事件，并跳过重复的 `trace.artifacts` usage 快照。

Codex 数据来源：

- `~/.codex/sqlite/state_5.sqlite`

Codex 不按线程级 `tokens_used` 汇总，因为该字段是线程累计值，跨天继续使用会把历史量算到当天。TokenMeter 读取 rollout JSONL 中每次 `token_count` 的 `last_token_usage`，按事件时间归档到对应日期。

ZCode 数据来源：

- `~/.zcode/cli/db/db.sqlite`

ZCode 读取 `model_usage` 表，并把缓存 token 从 input 中拆出，避免重复计入总量。

WorkBuddy 数据来源：

- `~/.workbuddy/projects/**/*.jsonl`

Claude Code 数据来源：

- `~/.claude/projects/**/*.jsonl`

这两个 JSONL 来源只读取 usage、模型、时间、cwd、session 等元数据，不读取正文。

## 安全说明

- 不要提交 `.env`、API token、SQLite 数据库、日志或本机采集结果。
- `data/*.sqlite`、`*.db`、`*.log` 等运行产物已经通过 `.gitignore` 排除。
- 对公网部署时建议放在 HTTPS 反向代理、Tailscale、SSH 隧道或内网环境后面。
- `TOKENMETER_TOKEN` 请使用足够长的随机字符串。

## 开发与测试

运行测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

运行语法检查：

```sh
PYTHONPATH=src python3 -m py_compile src/tokenmeter/*.py
```

## 项目结构

```text
src/tokenmeter/
  collectors.py   # 各 Agent 采集逻辑
  records.py      # 统一 usage record 数据结构
  storage.py      # SQLite 存储和每日聚合
  summary.py      # 汇总和表格输出
  server.py       # HTTP API 和 Web 页面
  __main__.py     # CLI 入口

tests/
  test_tokenmeter.py

docs/
  tokenmeter.md   # 设计和部署说明
```

## 当前版本

当前版本为 `0.1.0`，重点覆盖本地采集、中心上传、SQLite 汇总和 Web 看板。
