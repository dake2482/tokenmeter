---
schema_version: 1
id: TOKENMETER-004
project_id: TOKENMETER
title: 发布 TokenMeter 到 GitHub 并完善中文 README
status: DONE
priority: P1
executor: codex
task_id: codex-tokenmeter-github-publish-20260712
branch: main
worktree: local
dependencies:
  - TOKENMETER-001
updated_at: '2026-07-12T16:02:20+08:00'
next_action: null
evidence:
  - README 已整理为中文 GitHub 首页说明，覆盖项目定位、核心能力、安装方式、数据源、看板、安全边界和开发命令。
  - "PYTHONPATH=src python3 -m unittest discover -s tests passed 23 tests."
  - "PYTHONPATH=src python3 -m py_compile src/tokenmeter/*.py passed."
  - "sh -n scripts/install.sh passed."
  - "git diff --check passed."
  - Secret pattern scan found no tracked token/key/private-key matches.
  - Runtime files `data/tokenmeter.sqlite` and `tmp/tokenmeter.log` are ignored and not staged.
  - Published to GitHub repository https://github.com/dake2482/tokenmeter on branch main.
  - Release preparation commit 13be2df was pushed to origin/main.
---

# Intent

将当前 TokenMeter 项目发布到 GitHub，并确保仓库首页 README 使用中文说明项目用途、安装方式、安全边界和开发验证命令。

## Acceptance criteria

- [x] GitHub 远端仓库可访问，且本地提交已推送到远端 `main`。
- [x] README 使用中文说明项目定位、核心能力、安装方式、数据源、看板、安全边界和开发命令。
- [x] 提交前确认运行数据库、日志、凭据和本机配置不会被纳入 Git。
- [x] Canonical TokenMeter validation passes.
- [x] 发布证据记录到本需求文件。

## Verification plan

- Run `git status --short --branch` and inspect the intended file list.
- Run `git ls-files -o --exclude-standard` and sensitive-file checks.
- Run the three canonical TokenMeter validation commands.
- Push `main` to the configured GitHub remote after validation passes.
