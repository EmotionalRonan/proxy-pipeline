# 每日免费代理节点 · GitHub Actions CI 版

把原来的「WorkBuddy 自动化 + 本机跑」迁移成 **GitHub Actions 每日定时跑**，
彻底绕开本机网络不稳定、WorkBuddy 文件沙箱、launchd/cron 被 App Sandbox 拦截等问题。

CI 跑在 `ubuntu-latest`，自带稳定出网 + 定时器，每天把验证可用节点提交回仓库。

---

## 目录结构（本目录直接作为仓库根）

```
outputs/                        ← 整个目录 = 一个 GitHub 仓库
├── .github/workflows/daily.yml  CI 定义（每天 UTC 23:00 = 北京 07:00）
├── run_daily_pipeline.py       主编排脚本
├── download_and_extract.py     下载 + 去重
├── geo_enrich.py               IP 地理标记（ip-api.com）
├── tcp_screen.py               TCP 可达性筛查
├── mihomo_test_v2.py           Mihomo HTTP 延迟实测
├── mihomo_setup.py             自动下载 mihomo 二进制（跨平台）
├── proxy_text_blocks.py        节点文本块工具
├── sync_to_obsidian.py         发布步骤（CI 模式写 dist/）
├── requirements.txt            Python 依赖
└── dist/                       ← CI 生成的产物（verified_proxies.yaml 等）
```

## 一键推到 GitHub

```bash
cd /Users/ronan/WorkBuddy/Claw/outputs
git init
git add .
git commit -m "proxy pipeline: init with GitHub Actions CI"
# 在 GitHub 新建一个空仓库（如 proxy-pipeline），然后：
git branch -M main
git remote add origin https://github.com/<你的用户名>/proxy-pipeline.git
git push -u origin main
```

推送后：仓库 **Settings → Actions → General → Workflow permissions** 确认是
「Read and write permissions」（默认通常已是，workflow 里也显式声明了 `permissions: contents: write`）。

之后每天自动跑，产物出现在仓库 `dist/`：
- `dist/verified_proxies.yaml` — Mihomo 实测可用节点（订阅用这个）
- `dist/tcp_reachable.yaml`    — TCP 可达节点

## 订阅链接

在 Clash / Mihomo / v2rayN 等客户端里填 raw URL：

```
https://raw.githubusercontent.com/<你的用户名>/proxy-pipeline/main/dist/verified_proxies.yaml
```

## 想沿用现有 obs 仓库的订阅链接？

如果你现在的订阅指向 `EmotionalRonan/obs` 的 raw 链接，可以直接把本目录内容
（含 `.github/`）推进 **obs 仓库根目录**，`dist/` 就会落在 obs 仓库，订阅链接不变。
只需把上面 `git remote add origin` 换成 obs 仓库地址即可。

## 调试 / 手动触发

- 在仓库 **Actions → Daily Proxy Node Update → Run workflow** 可手动跑一次。
- 本地模拟 CI 同步逻辑（不下载节点，只看发布步骤）：
  ```bash
  GITHUB_ACTIONS=true python sync_to_obsidian.py
  ```
  会把 `mihomo_verified_nodes.yaml` / `reachable_nodes.yaml` 复制到 `dist/`。

## 备注

- `sync_to_obsidian.py` 在非 CI 环境（本机）仍走原逻辑：复制到本地 Obsidian vault
  + 上传 dufs 局域网服务器 + git push，可用环境变量 `OBSIDIAN_TARGET_DIR` /
  `OBSIDIAN_REPO_DIR` / `DUFS_URL` 覆盖路径。
- 时区：GitHub Actions cron 是 UTC。`0 23 * * *` = 北京 07:00。如需调整改 daily.yml 即可。
- 仓库若 60 天无提交，GitHub 会自动暂停定时任务；手动触发一次即恢复。
