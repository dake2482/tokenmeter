# TokenMeter

TokenMeter 是一个自部署的 Agent token 用量统计工具。它把多台机器上的 Hermes、OpenClaw、Codex、ZCode、WorkBuddy、Claude Code 用量采集到一个中心 SQLite 数据库，并提供一个 Web 看板查看按工具、模型、服务器、Profile 和历史日期的统计。

它适合这样使用：

- 一台机器作为中心服务，运行 Web 看板和接收 API。
- 其他机器安装轻量上传器，每 15 分钟采集本机 token 用量并上传。
- 所有安装都可以通过一条命令完成，适合直接贴给 AI Agent 执行。

TokenMeter 只读取 token usage、模型、时间、cwd、session 等统计元数据，不读取消息正文、prompt、回复正文或密钥。

## AI 一键安装

把下面命令贴给要部署中心看板的 AI 或服务器终端：

```sh
curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh | sudo sh -s -- server
```

安装完成后会输出：

- Web 看板地址：默认 `http://<server>:18888/`
- API token：默认自动生成，打开页面时填入
- 给其他机器安装上传器的示例命令

如果之后忘记 token，root Linux 安装默认可以在 `/etc/tokenmeter.env` 中查看。

如果你想自己指定 token：

```sh
curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh \
  | sudo env TOKENMETER_TOKEN="change-this-long-random-token" sh -s -- server
```

如果只想在本机或内网试用，不启用 API token：

```sh
curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh \
  | sudo env TOKENMETER_DISABLE_TOKEN=1 sh -s -- server
```

在其他机器安装上传器，把 `TOKENMETER_SERVER` 和 `TOKENMETER_TOKEN` 换成你的中心服务地址和 token：

```sh
curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh \
  | TOKENMETER_SERVER="https://your-tokenmeter.example.com" TOKENMETER_TOKEN="your-token" sh -s -- agent
```

如果目标机器需要 root 权限安装 systemd timer：

```sh
curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh \
  | sudo env TOKENMETER_SERVER="https://your-tokenmeter.example.com" TOKENMETER_TOKEN="your-token" sh -s -- agent
```

默认安装当前 GitHub 仓库 `dake2482/tokenmeter` 的 `main` 分支。Fork 后可以这样安装自己的版本：

```sh
curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh \
  | sudo env TOKENMETER_REPO="your-name/tokenmeter" TOKENMETER_REF="main" sh -s -- server
```

## 安装脚本参数

`scripts/install.sh` 支持两个模式：

- `server`：安装中心 Web 看板和本机自动采集服务。
- `agent`：安装上传器，立即上传最近 30 天数据，并创建每 15 分钟上传最近 1 天数据的定时任务。

常用环境变量：

- `TOKENMETER_REPO`：GitHub 仓库，默认 `dake2482/tokenmeter`。
- `TOKENMETER_REF`：分支、tag 或 commit SHA，默认 `main`。
- `TOKENMETER_DIR`：安装目录。root Linux 默认 `/opt/tokenmeter`，普通用户默认 `~/.local/share/tokenmeter`。
- `TOKENMETER_BIND`：中心服务监听地址，默认 `0.0.0.0:18888`。
- `TOKENMETER_DB`：中心 SQLite 数据库路径，root Linux 默认 `/var/lib/tokenmeter/tokenmeter.sqlite`。
- `TOKENMETER_TOKEN`：API Bearer token。server 模式未设置时会自动生成。
- `TOKENMETER_DISABLE_TOKEN=1`：server 模式禁用 API token，仅建议本机或内网试用。
- `TOKENMETER_SERVER`：agent 模式上传目标，例如 `https://your-tokenmeter.example.com`。
- `TOKENMETER_HOST`：上报主机名，默认 `hostname`。
- `TOKENMETER_INTERVAL`：上传间隔秒数，默认 `900`。
- `TOKENMETER_BOOTSTRAP_SINCE`：首次上传窗口，默认 `30d`。
- `TOKENMETER_SINCE`：后续定时上传窗口，默认 `1d`。
- `TOKENMETER_AGENTS`：采集 Agent 列表，默认 `hermes,openclaw,codex,zcode,workbuddy,claude`。

## 手动运行

克隆仓库后可以直接用 Python 运行，不需要额外依赖：

```sh
git clone https://github.com/dake2482/tokenmeter.git
cd tokenmeter
PYTHONPATH=src python3 -m tokenmeter collect --since 24h
```

启动中心看板：

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter serve \
  --bind 0.0.0.0:18888 \
  --db data/tokenmeter.sqlite
```

从另一台机器上传：

```sh
TOKENMETER_TOKEN="change-me" PYTHONPATH=src python3 -m tokenmeter upload \
  --server http://your-tokenmeter-server:18888 \
  --host "$(hostname)" \
  --since 7d
```

查看中心库汇总：

```sh
PYTHONPATH=src python3 -m tokenmeter summary \
  --db data/tokenmeter.sqlite \
  --since 7d \
  --group-by host,agent,profile,model
```

## 支持的数据源

- Hermes：`~/.hermes/state.db` 和 `~/.hermes/profiles/<profile>/state.db`
- OpenClaw：`~/.openclaw/agents/<profile>/sessions/*.trajectory.jsonl`
- Codex：`~/.codex/sqlite/state_5.sqlite` 以及 rollout JSONL 中的 `token_count.last_token_usage`
- ZCode：`~/.zcode/cli/db/db.sqlite` 的 `model_usage`
- WorkBuddy：`~/.workbuddy/projects/**/*.jsonl` 和 `~/.workbuddy/traces/**/*.json`
- Claude Code：`~/.claude/projects/**/*.jsonl`

Codex 不按线程级 `tokens_used` 汇总，因为该字段是线程累计值，跨天继续使用会把历史量算到当天。TokenMeter 读取 rollout JSONL 中每次 `token_count` 事件的增量 usage，并按事件时间归档。

Codex 可能连续写入累计值未变化的 `token_count` 快照。TokenMeter 使用 `total_token_usage.total_tokens` 识别并跳过这些重复快照；上传器同时把重复记录 ID 发给中心服务，以精确清理旧版本已经入库的重复数据。

Codex 的 subagent/fork rollout 会继承父线程历史。TokenMeter 会删除这些继承记录，避免跨线程重复累计。若本机已通过 TokenRank 官方安装方式安装 `~/.local/bin/opentoken`，TokenMeter 会只读调用 `opentoken preview` 校准 Codex 的日/模型总量，同时按根线程活动保留 Profile 与 15 分钟分布；没有该程序时采用保守的根线程口径。

OpenClaw 只统计 `model.completed` 事件，并跳过重复的 `trace.artifacts` usage 快照。

## Web 看板

看板包含：

- 顶部筛选：按 Agent 和时间范围筛选。
- 今日/昨日/区间数据：展示当前筛选范围的总 token、估算成本、活跃天数。
- 用量占比：真实 SVG 饼图，悬停显示具体 token 和占比。
- 按模型：展示当前筛选范围内各模型占比。
- 按服务器：展示当前筛选范围内各上报服务器占比。
- 按 Profile：展示 OpenClaw 和 Hermes 的 profile 占比。
- 历史明细：展示完整历史日期数据，不随顶部时间范围收缩。

## 安全说明

- 不要提交 `.env`、API token、SQLite 数据库、日志或本机采集结果。
- `data/*.sqlite`、`*.db`、`*.log` 等运行产物已经通过 `.gitignore` 排除。
- 公网部署建议放在 HTTPS 反向代理、Tailscale、SSH 隧道或内网环境后面。
- `TOKENMETER_TOKEN` 请使用足够长的随机字符串。
- 一键安装脚本会把 token 写入本机服务环境文件；这些文件只留在被安装的机器上，不会提交到仓库。

## 开发与测试

运行测试：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

运行语法检查：

```sh
PYTHONPATH=src python3 -m py_compile src/tokenmeter/*.py
sh -n scripts/install.sh
```

## 项目结构

```text
scripts/install.sh  # 通用一键安装入口

src/tokenmeter/
  collectors.py     # 各 Agent 采集逻辑
  records.py        # 统一 usage record 数据结构
  storage.py        # SQLite 存储和每日聚合
  summary.py        # 汇总和表格输出
  server.py         # HTTP API 和 Web 页面
  __main__.py       # CLI 入口

tests/
  test_tokenmeter.py
```
