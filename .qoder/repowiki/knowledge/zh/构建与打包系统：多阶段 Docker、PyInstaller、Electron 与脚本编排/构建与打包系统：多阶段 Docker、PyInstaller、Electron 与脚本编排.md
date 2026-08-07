---
kind: build_system
name: 构建与打包系统：多阶段 Docker、PyInstaller、Electron 与脚本编排
category: build_system
scope:
    - '**'
source_files:
    - requirements.txt
    - pyproject.toml
    - docker/Dockerfile
    - docker/docker-compose.yml
    - docker/entrypoint.sh
    - scripts/build-backend-macos.sh
    - scripts/build-desktop-macos.sh
    - scripts/build-all-macos.sh
    - scripts/test.sh
    - apps/dsa-web/package.json
    - apps/dsa-desktop/package.json
---

## 1. 使用的构建系统与工具链
- **Python 后端**：依赖通过 `requirements.txt` 管理，使用 `pip install -r requirements.txt` 安装；代码格式化工具为 Black（行宽 120）和 isort，安全扫描使用 bandit。
- **前端 Web UI**：基于 Vite + React + TypeScript，构建入口在 `apps/dsa-web/package.json`，`npm run build` 输出静态资源到 `static/`。
- **桌面客户端**：基于 Electron + electron-builder，打包产物为 macOS 的 `.dmg` 与 Windows 的 NSIS 安装包，版本由 `apps/dsa-desktop/package.json` 的 `version` 字段控制。
- **容器化**：Docker 多阶段镜像构建，Node 20 负责前端构建，Python 3.11-slim-bookworm 作为运行时，包含 wkhtmltopdf 等系统依赖。
- **CI/CD**：GitHub Actions 工作流位于 `.github/workflows/`（目录存在但当前仓库未包含具体 workflow 文件），门禁脚本 `scripts/ci_gate.sh` 提供 CI 检查能力。

## 2. 核心构建文件与位置
- **依赖声明**：`requirements.txt`（Python）、`apps/dsa-web/package.json`（Web）、`apps/dsa-desktop/package.json`（Desktop）
- **Docker 配置**：`docker/Dockerfile`、`docker/docker-compose.yml`、`docker/entrypoint.sh`
- **构建脚本**：
  - `scripts/build-backend-macos.sh`：PyInstaller 打包后端可执行文件
  - `scripts/build-desktop-macos.sh`：Electron 打包桌面端
  - `scripts/build-all-macos.sh`：一键调用上述两个脚本
  - `scripts/test.sh`：统一的测试入口，支持多种测试场景
- **代码质量**：`pyproject.toml`（Black/isort/bandit 配置）

## 3. 构建架构与流程
### 3.1 后端构建（PyInstaller）
```bash
# 1. 先构建前端静态资源
npm run build  # apps/dsa-web/

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. PyInstaller 打包
python -m PyInstaller --name stock_analysis --onedir \
  --hidden-import=...（显式声明动态导入模块）\
  --add-data "static:static" --add-data "strategies:strategies" \
  main.py
```
输出：`dist/backend/stock_analysis/` 目录，包含可执行文件和内嵌的 static/strategies 资源。

### 3.2 桌面端构建（Electron）
```bash
# 前置条件：必须先运行 build-backend-macos.sh
bash scripts/build-backend-macos.sh

# 然后打包桌面端
bash scripts/build-desktop-macos.sh
```
输出：`apps/dsa-desktop/dist/mac/*.dmg`（macOS）或 Windows NSIS 安装包。

### 3.3 Docker 镜像构建
```bash
docker build -f docker/Dockerfile -t daily-stock-analysis .
```
多阶段构建：
- **阶段 1 (web-builder)**：Node 20 环境构建前端静态资源
- **阶段 2 (python runtime)**：Python 3.11 基础镜像，安装依赖并复制应用代码

### 3.4 服务启动模式
- **定时任务模式**：`python main.py --schedule`（默认 CMD）
- **FastAPI 服务模式**：`python main.py --serve-only --host 0.0.0.0 --port ${API_PORT}`
- **双模式同时运行**：docker-compose 中定义 `analyzer` 和 `server` 两个服务

## 4. 关键约定与约束
### 4.1 依赖版本约束
- Python：`python-dotenv>=1.0.0`、`fastapi>=0.109.0`、`uvicorn[standard]>=0.27.0` 等
- Node.js：要求 `node >=20.19.0 <27`，`npm >=10`（通过 engines 字段强制）
- 特殊版本锁定：`tiktoken>=0.8.0,<0.12.0`（避免插件注册问题）、`litellm>=1.80.10,!=1.82.7,!=1.82.8,<2.0.0`

### 4.2 构建产物校验
- **静态资源完整性检查**：`check_static_assets.py` 验证打包前后 static 目录引用一致性
- **策略文件数量校验**：确保打包后的 strategies YAML 文件数量与源码一致
- **可执行文件健康检查**：打包后执行 `--help` 验证可启动性

### 4.3 安全与权限
- Docker 容器以非 root 用户 `dsa`（UID 1000）运行
- 数据目录 `/app/data`、`/app/logs`、`/app/reports` 通过 volume 持久化
- entrypoint 脚本自动修复 bind mount 目录权限

### 4.4 环境变量配置
- 时区：`TZ=Asia/Shanghai`
- API 端口：`API_PORT`（默认 8000）
- 数据库路径：`DATABASE_PATH=/app/data/stock_analysis.db`
- 日志目录：`LOG_DIR=/app/logs`

## 5. 测试体系
- **单元测试**：pytest 框架，测试文件位于 `tests/` 目录
- **端到端测试**：Playwright 用于 Web UI 自动化测试
- **冒烟测试**：`scripts/test.sh` 提供多种测试场景（market/a-stock/us-stock/hk-stock/mixed/single/dry-run/full/quick/all）
- **静态检查**：flake8 语法检查，Black 代码格式化

## 6. 发布流程
- **桌面端更新**：electron-updater 集成 GitHub Releases 自动更新
- **Docker 镜像**：标准 Docker Hub 发布流程
- **版本管理**：桌面端版本由 `apps/dsa-desktop/package.json` 的 `version` 字段控制，构建产物命名包含版本号

该构建系统采用多语言混合架构（Python + Node.js + Electron），通过脚本编排实现从源码到可部署产物的完整流水线，具备完善的校验机制和跨平台支持。