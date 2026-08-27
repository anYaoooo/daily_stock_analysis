---
kind: build_system
name: 多产物构建与发布流水线（PyInstaller + Electron + Docker + GitHub Actions）
category: build_system
scope:
    - '**'
source_files:
    - scripts/build-all-macos.sh
    - scripts/build-backend-macos.sh
    - scripts/build-desktop-macos.sh
    - scripts/build-all.ps1
    - scripts/build-backend.ps1
    - docker/Dockerfile
    - docker/entrypoint.sh
    - docker/docker-compose.yml
    - .github/workflows/ci.yml
    - .github/workflows/create-release.yml
    - .github/workflows/desktop-release.yml
    - .github/workflows/ghcr-dockerhub.yml
    - .github/workflows/docker-publish.yml
    - apps/dsa-web/package.json
    - apps/dsa-web/vite.config.ts
    - apps/dsa-web/vitest.config.ts
    - apps/dsa-web/playwright.config.ts
    - apps/dsa-desktop/package.json
    - requirements.txt
    - .github/requirements-ci.txt
    - pyproject.toml
    - scripts/test.sh
    - scripts/ci_gate.sh
    - scripts/check_static_assets.py
---

## 1. 使用的构建系统/工具

- **后端可执行打包**：使用 PyInstaller 将 Python 应用（`main.py`）打包为单目录可执行 `stock_analysis`，输出到 `dist/backend/stock_analysis`。macOS 与 Windows 分别通过 `scripts/build-backend-macos.sh` 和 `scripts/build-backend.ps1` 驱动。
- **桌面客户端**：Electron + electron-builder，位于 `apps/dsa-desktop`，`package.json` 中定义 NSIS（Windows）与 dmg（macOS）目标，并通过 `extraResources` 把预编译的 `dist/backend/stock_analysis` 嵌入安装包。
- **Web 前端**：Vite + React + TypeScript，位于 `apps/dsa-web`，构建产物输出到仓库根 `static/`，由后端以静态资源方式提供；Docker 镜像采用 Node 20 阶段先构建前端再复制到最终镜像。
- **容器化**：`docker/Dockerfile` 使用多阶段构建（`node:20-slim` → `python:3.11-slim-bookworm`），安装 wkhtmltopdf、字体等运行时依赖，以非 root 用户 `dsa` 运行，默认命令 `python main.py --schedule`。
- **CI/CD**：GitHub Actions 工作流集中在 `.github/workflows/`：
  - `ci.yml`：PR 触发，包含 AI 治理检查、Python 语法/Flake8/离线测试门控、Docker 镜像构建与导入冒烟、前端 lint+build（路径过滤）。
  - `create-release.yml`：推送 `v*.*.*` 标签时自动生成 release notes 并发布 GitHub Release。
  - `desktop-release.yml`、`ghcr-dockerhub.yml`、`docker-publish.yml`：负责桌面端与镜像发布。
  - `network-smoke.yml`、`pr-review.yml`、`stale.yml`、`auto-tag.yml`、`00-daily-analysis.yml`：网络冒烟、PR review、过期 issue、自动打 tag、每日分析任务。
- **本地测试脚本**：`scripts/test.sh` 提供 `market/a-stock/hk-stock/us-stock/mixed/single/dry-run/full/quick/all/syntax/flake8` 等多场景入口；`scripts/ci_gate.sh` 被 CI 调用，封装 `syntax / flake8 / deterministic / offline-tests` 子命令。

## 2. 关键文件

- 构建脚本：`scripts/build-all-macos.sh`、`scripts/build-backend-macos.sh`、`scripts/build-desktop-macos.sh`、`scripts/build-all.ps1`、`scripts/build-backend.ps1`
- 容器：`docker/Dockerfile`、`docker/docker-compose.yml`、`docker/entrypoint.sh`
- 前端工程：`apps/dsa-web/package.json`、`apps/dsa-web/vite.config.ts`、`apps/dsa-web/vitest.config.ts`、`apps/dsa-web/playwright.config.ts`
- 桌面工程：`apps/dsa-desktop/package.json`、`apps/dsa-desktop/main.js`、`apps/dsa-desktop/preload.js`
- 依赖清单：`requirements.txt`、`.github/requirements-ci.txt`、`pyproject.toml`（black/isort/bandit 配置）
- CI 工作流：`.github/workflows/ci.yml`、`.github/workflows/create-release.yml`、`.github/workflows/desktop-release.yml`、`.github/workflows/ghcr-dockerhub.yml`、`.github/workflows/docker-publish.yml`
- 测试与门禁：`scripts/test.sh`、`scripts/ci_gate.sh`、`scripts/check_static_assets.py`、`scripts/check_ai_assets.py`

## 3. 架构与约定

- **前后端解耦构建**：后端构建前先 `cd apps/dsa-web && npm run build` 生成 `static/`，再由 PyInstaller 通过 `--add-data static:static` 与 `--collect-data litellm/tiktoken` 一并打入可执行；桌面端再通过 `extraResources` 把 `dist/backend/stock_analysis` 嵌入安装包，形成“后端可执行 → 桌面包”的两级产物关系。
- **平台对称实现**：同一套 PyInstaller 参数在 macOS shell 与 Windows PowerShell 中各有一份等价实现（hidden-imports 列表、策略 YAML 校验、静态资源校验逻辑一致），确保跨平台构建结果对等。
- **构建产物校验内联**：每个后端构建脚本在 PyInstaller 完成后都会执行：
  - 检查可执行入口存在且可运行（`--help`）。
  - 用 `check_static_assets.py` 验证打包后的 `_internal/static` 或 `static` 目录引用完整性。
  - 统计 `strategies/*.yaml` 数量并与源码目录对比，防止遗漏策略文件。
- **Docker 安全基线**：镜像内创建 `dsa` 用户（UID/GID 1000），数据/日志/报告目录挂载为 volume，`entrypoint.sh` 在 root 下修复 bind mount 权限后通过 `gosu` 降权执行，健康检查指向 `/api/health` 与 `/health`。
- **版本与发布**：桌面端版本号来自 `apps/dsa-desktop/package.json` 的 `version`（如 `3.21.0`），Windows 安装包命名遵循 `daily-stock-analysis-windows-installer-v${version}.${ext}`；Release 由 `create-release.yml` 监听 `v*.*.*` 标签触发，通过 `.github/scripts/build_release_notes.py` 生成 release body。
- **Node 版本约束**：`apps/dsa-web/package.json` 声明 `engines.node >=20.19.0 <27`，CI 使用 `actions/setup-node@v6` 固定 `node-version: '20'`，保证构建环境一致。

## 4. 约定与约束

- **Python 环境**：CI 与 Docker 均基于 Python 3.11；本地后端构建要求 Python 3.10+，可通过 `PYTHON_BIN` 环境变量指定解释器。
- **依赖缓存**：CI 使用 `pip` 缓存（`cache-dependency-path: requirements.txt, .github/requirements-ci.txt`）与 `npm` 缓存（`apps/dsa-web/package-lock.json`）加速构建。
- **构建失败即中止**：所有脚本使用 `set -euo pipefail` 或 `$ErrorActionPreference = 'Stop'`，任一步骤失败立即退出。
- **桌面依赖增量重建**：`build-desktop-macos.sh` 通过计算 `package-lock.json` 的 SHA-256 并写入 `node_modules/.dsa-package-lock.sha256`，仅在锁文件或 `electron-updater` 缺失时重新 `npm install`。
- **macOS 签名禁用**：桌面构建显式设置 `CSC_IDENTITY_AUTO_DISCOVERY=false`，避免本地未配置证书导致构建失败。
- **策略与静态资源一致性**：构建流程强制要求打包后的策略 YAML 数量与源码一致、静态资源引用完整，否则直接报错终止。
- **Docker 卷与权限**：`/app/data`、`/app/logs`、`/app/reports`、`/home/dsa/.longbridge` 为可写目录；若宿主机挂载目录属主不匹配，entrypoint 会尝试 `chown -R` 并提示 NFS/只读挂载问题。
- **CI 并发控制**：`concurrency.group: ci-${{ github.event.pull_request.number || github.ref }}` 配合 `cancel-in-progress: true`，避免重复 PR 并行构建堆积。
- **测试分层**：CI 仅运行 `offline-tests` 与 `deterministic` 子集；需要网络的端到端测试通过 `scripts/test.sh` 的 `dry-run/quick/full` 等模式在开发者本地执行。