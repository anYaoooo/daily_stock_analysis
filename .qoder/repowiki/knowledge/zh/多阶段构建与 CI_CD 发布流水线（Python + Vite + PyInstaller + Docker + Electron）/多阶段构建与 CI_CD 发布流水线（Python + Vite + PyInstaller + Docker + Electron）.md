---
kind: build_system
name: 多阶段构建与 CI/CD 发布流水线（Python + Vite + PyInstaller + Docker + Electron）
category: build_system
scope:
    - '**'
source_files:
    - docker/Dockerfile
    - docker/docker-compose.yml
    - docker/entrypoint.sh
    - scripts/build-backend-macos.sh
    - scripts/build-desktop-macos.sh
    - scripts/build-all-macos.sh
    - scripts/test.sh
    - scripts/ci_gate.sh
    - .github/workflows/ci.yml
    - .github/workflows/docker-publish.yml
    - .github/workflows/create-release.yml
    - pyproject.toml
    - requirements.txt
    - apps/dsa-web/package.json
---

## 1. 构建系统与工具链

本项目采用**多语言、多产物**的构建体系：
- **后端 Python 服务**：依赖 `requirements.txt`，通过 `pip` 安装；本地打包使用 **PyInstaller**（`--onedir --noconsole`），入口为 `main.py`。
- **Web 前端**：位于 `apps/dsa-web`，基于 **Vite + React + TypeScript**，构建命令为 `tsc -b && vite build`，输出静态资源到根目录 `static/`。
- **桌面端**：Electron 应用位于 `apps/dsa-desktop`，使用 **electron-builder** 生成 macOS DMG，并内置 `electron-updater` 实现自更新。
- **容器化**：`docker/Dockerfile` 使用**多阶段构建**——第一阶段 `node:20-slim` 编译前端，第二阶段 `python:3.11-slim-bookworm` 运行后端，最终镜像暴露 8000 端口并通过 `entrypoint.sh` 以非 root 用户 `dsa` (UID 1000) 启动。
- **CI/CD**：GitHub Actions 工作流位于 `.github/workflows/`，包含 PR 检查、Docker 镜像构建推送、Release 创建等。

## 2. 关键文件与职责

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | 定义 Black/isort/bandit 代码规范配置（行宽 120，目标 py310-312） |
| `requirements.txt` | Python 运行时依赖声明（FastAPI、litellm、ccxt、pandas 等） |
| `scripts/build-backend-macos.sh` | 后端打包脚本：先 `npm run build` 生成静态资源，再调用 PyInstaller 打包，校验 hidden imports、策略 YAML 数量一致性 |
| `scripts/build-desktop-macos.sh` | 桌面端打包：依赖已存在的 `dist/backend/stock_analysis`，用 electron-builder 生成 DMG，支持 `DSA_MAC_ARCH=x64|arm64` |
| `scripts/build-all-macos.sh` | 一键编排：依次执行后端与桌面端构建 |
| `scripts/test.sh` | 本地端到端测试脚本，支持 market/a-stock/hk/us/mixed/single/dry-run/full/quick/all 等场景 |
| `scripts/ci_gate.sh` | CI 门禁统一入口，封装 syntax/flake8/deterministic/offline-tests 子任务 |
| `docker/Dockerfile` | 多阶段镜像：web-builder → python 运行环境，安装 wkhtmltopdf 等系统依赖，HEALTHCHECK 指向 `/api/health` |
| `docker/docker-compose.yml` | 双服务编排：`analyzer`（定时模式）与 `server`（FastAPI 模式），挂载 data/logs/reports/strategies 卷 |
| `docker/entrypoint.sh` | 容器启动前修复 bind mount 权限，检测数据目录可写性，再以 `dsa` 用户 exec 应用进程 |
| `.github/workflows/ci.yml` | PR 触发：AI 治理检查 → backend-gate（flake8+离线测试）→ docker-build（导入冒烟）→ web-gate（lint+build） |
| `.github/workflows/docker-publish.yml` | 仅对 `v*.*.*` 标签触发：验证 annotated tag message → 构建 linux/amd64+linux/arm64 镜像 → 推送到 GHCR 与可选 Docker Hub |
| `.github/workflows/create-release.yml` | 推送 annotated tag 时自动生成 GitHub Release，release notes 由 `.github/scripts/build_release_notes.py` 生成 |
| `apps/dsa-web/package.json` | 前端工程配置，限定 Node >=20.19.0 <27，脚本含 dev/build/lint/test/test:smoke(Playwright) |

## 3. 架构与约定

- **构建顺序**：必须先构建前端静态资源（`apps/dsa-web` → `static/`），再打包后端或构建 Docker 镜像。PyInstaller 通过 `--add-data "static:static"` 将静态资源嵌入可执行文件。
- **隐藏导入清单**：后端打包需显式声明 `hidden_imports`（multipart、tiktoken、uvicorn、api.v1.*、src.services.* 等），缺失会导致运行时 import 失败。
- **策略文件完整性校验**：打包后统计 `_internal/strategies/*.yaml` 数量必须等于源码 `strategies/` 下 YAML 数量，否则构建失败。
- **静态资源一致性校验**：构建前后均调用 `scripts/check_static_assets.py` 比对 `static/assets/` 中 JS/CSS 文件名是否完整引用，防止 Vite 哈希名变更导致 404。
- **桌面端依赖缓存**：`build-desktop-macos.sh` 通过计算 `package-lock.json` 的 SHA-256 写入 `node_modules/.dsa-package-lock.sha256` 作为增量缓存标记，仅在 lock 变化或缺少 `electron-updater` 时重新 `npm install`。
- **Docker 安全基线**：镜像内创建 `dsa` 用户组/用户（UID/GID 1000），所有持久化目录 (`/app/data`, `/app/logs`, `/app/reports`, `/home/dsa/.longbridge`) 在 entrypoint 中以 root 预检并 chown/chmod 修复后再降权执行。
- **版本与发布**：版本号来自 Git tag（semver `vX.Y.Z`），发布流程要求 tag 必须是 annotated tag 且带有 release notes 正文，否则 docker-publish 拒绝。
- **CI 并行与缓存**：PR 构建使用 `concurrency` 取消旧任务；Node 依赖通过 `npm ci` + `cache-dependency-path: apps/dsa-web/package-lock.json` 缓存；Python 依赖通过 pip cache 加速。

## 4. 约束与规则

- **Python 版本**：CI 固定使用 Python 3.11；Black 目标版本覆盖 py310/py311/py312。
- **Node 版本**：前端要求 `engines.node >=20.19.0 <27`，CI 使用 Node 20。
- **Docker 基础镜像**：后端固定 `python:3.11-slim-bookworm`，前端构建固定 `node:20-slim`。
- **环境变量**：容器默认 `TZ=Asia/Shanghai`、`WEBUI_HOST=0.0.0.0`、`API_PORT=8000`、`DATABASE_PATH=/app/data/stock_analysis.db`、`LOG_DIR=/app/logs`；生产部署通过 `docker-compose.yml` 的 `env_file: ../.env` 注入。
- **健康检查**：镜像 HEALTHCHECK 轮询 `/api/health` 和 `/health`，失败则返回非零退出码。
- **网络隔离**：CI 的 `offline-tests` 与 `deterministic` 步骤确保无外部网络依赖也能通过核心测试。
- **发布门禁**：`docker-publish.yml` 强制要求 annotated tag 带 release notes 正文，否则直接 exit 1。
- **策略与模板**：Jinja2 模板位于 `templates/`，Markdown 转图片依赖宿主机/镜像中的 `wkhtmltopdf` 二进制。
- **桌面端签名**：macOS 构建禁用自动签名（`CSC_IDENTITY_AUTO_DISCOVERY=false`），通过 `--publish never` 仅生成本地 DMG。