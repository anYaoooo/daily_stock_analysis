---
kind: dependency_management
name: 多语言依赖管理：Python requirements.txt + npm lockfile + CI 缓存策略
category: dependency_management
scope:
    - '**'
source_files:
    - requirements.txt
    - .github/requirements-ci.txt
    - pyproject.toml
    - apps/dsa-web/package.json
    - apps/dsa-web/package-lock.json
    - apps/dsa-desktop/package.json
    - apps/dsa-desktop/package-lock.json
    - .github/workflows/ci.yml
    - .github/workflows/docker-publish.yml
    - .github/workflows/network-smoke.yml
    - docker/Dockerfile
---

## 1. 使用的系统/工具

本项目是一个多语言仓库，包含 Python 后端、React Web 前端和 Electron 桌面端，因此采用**分模块的依赖管理方案**：

- **Python 后端**：使用 `pip` + `requirements.txt` 声明依赖，无 `poetry`/`pdm`/`uv` 等现代包管理器；通过 `.github/requirements-ci.txt` 在 CI 中扩展测试与 lint 工具。
- **Web 前端（apps/dsa-web）**：使用 `npm` + `package.json` + `package-lock.json`（lockfileVersion 3），并通过 `npm ci` 进行确定性安装。
- **Electron 桌面端（apps/dsa-desktop）**：同样使用 `npm` + `package.json` + `package-lock.json`，并借助 `electron-builder` 打包发布到 GitHub Releases。
- **Docker 构建**：`docker/Dockerfile` 基于官方镜像，通过 `pip install -r requirements.txt` 安装依赖。
- **CI 缓存**：GitHub Actions 对 pip 和 npm 均启用缓存（`cache: 'pip'`、`cache-dependency-path: apps/dsa-web/package-lock.json`）。

## 2. 关键文件

- `requirements.txt` — Python 后端全部运行时依赖，按功能分组注释（Core / Data processing / AI analysis / Search/news / Network requests / Discord bot / Report template / FastAPI）。
- `.github/requirements-ci.txt` — 通过 `-r ../requirements.txt` 引入主依赖，再追加 `flake8`、`pytest` 作为 CI 专用工具。
- `pyproject.toml` — 仅配置代码风格工具（black、isort、bandit），**不声明项目依赖**。
- `apps/dsa-web/package.json` + `apps/dsa-web/package-lock.json` — Web 前端依赖及锁定版本。
- `apps/dsa-desktop/package.json` + `apps/dsa-desktop/package-lock.json` — Electron 桌面端依赖及打包配置。
- `.github/workflows/ci.yml` — 统一入口：backend-gate 用 pip 安装，web-gate 用 npm ci 安装。
- `docker/Dockerfile` — 容器化时重新安装依赖。

## 3. 架构与约定

### Python 依赖版本约束策略
`requirements.txt` 中的版本约束体现“最小兼容 + 主动排除已知坏版本”的模式：
- 使用 `>=` 指定最低版本（如 `fastapi>=0.109.0`、`litellm>=1.80.10`）。
- 对存在已知问题的上游包使用显式排除：`litellm>=1.80.10,!=1.82.7,!=1.82.8,<2.0.0`，注释说明避免未来 major 破坏。
- 对易变依赖加上限：`scikit-learn>=1.4.0,<2.0.0`、`tiktoken>=0.8.0,<0.12.0`（注释引用 issue #537）、`websockets>=12.0,<16.0`（避免 asyncio 握手噪音）。
- 可选依赖以 extras 形式声明：`httpx[socks]`。
- 内置库（如 SQLite）明确注释“不需要额外包”。

### 前端依赖管理
- `apps/dsa-web/package.json` 通过 `engines.node >=20.19.0 <27` 和 `engines.npm >=10` 约束运行环境。
- 所有依赖使用 `^` 语义化版本范围，由 `package-lock.json` 锁定实际解析版本。
- CI 使用 `npm ci` 而非 `npm install`，确保安装完全可重现。

### 桌面端依赖与发布
- `apps/dsa-desktop/package.json` 将 `electron-updater` 作为运行时依赖，用于应用内自动更新。
- `electron-builder` 配置中将后端二进制 `../../dist/backend/stock_analysis` 作为 `extraResources` 打包进安装包。
- Windows 发布目标为 NSIS 安装包，发布到 GitHub Releases（owner: ZhuLinsen, repo: daily_stock_analysis）。

### CI 中的依赖安装流程
- backend-gate job：`setup-python@v6` 安装 Python 3.11 → `pip install --upgrade pip` → 重试机制安装 `.github/requirements-ci.txt`（最多 3 次，间隔 15 秒）→ 执行 `scripts/ci_gate.sh` 的 syntax/flake8/deterministic/offline-tests。
- web-gate job：仅在 `apps/dsa-web/**` 有变更时触发 → `setup-node@v6` 安装 Node 20 → `npm ci` → `npm run lint` → `npm run build`。
- docker-publish、network-smoke、pr-review 等 workflow 也各自独立 `pip install -r requirements.txt`。

## 4. 约定与约束

- **Python 没有 lockfile**：仓库未提交 `requirements.lock`/`Pipfile.lock`/`poetry.lock`，依赖版本由 `requirements.txt` 中的宽松范围决定，确定性依赖由 CI 缓存和 Docker 镜像保证。
- **CI 依赖与生产依赖分离**：通过 `.github/requirements-ci.txt` 引入主依赖后再追加 flake8、pytest，避免污染生产依赖集。
- **版本回滚策略**：对不稳定上游包使用 `<major` 上限或 `!=` 排除特定版本（如 litellm、tiktoken、websockets、scikit-learn），并在注释中记录原因。
- **Node 版本锁定**：通过 `engines` 字段和 CI 的 `node-version: '20'` 双重约束。
- **Docker 重建依赖**：每次 `docker build` 都会重新 `pip install -r requirements.txt`，不依赖宿主机缓存。
- **无私有注册表**：未发现 `pip index-url`、`.pypirc`、`~/.npmrc` 或私有 registry 配置，所有依赖来自 PyPI/npm 公共源。
- **无 vendoring**：未使用 `pip install --no-binary` 或 vendor 目录，第三方包直接通过包管理器安装。
- **脚本工具集中管理**：lint/test/build 等辅助命令集中在 `scripts/` 目录（如 `ci_gate.sh`、`check_env.py`、`build-backend-macos.sh`），由 CI 调用。