---
kind: dependency_management
name: 多语言依赖管理：Python requirements.txt + npm lockfile + 桌面端 electron-builder
category: dependency_management
scope:
    - '**'
source_files:
    - requirements.txt
    - pyproject.toml
    - setup.cfg
    - .github/requirements-ci.txt
    - apps/dsa-web/package.json
    - apps/dsa-web/package-lock.json
    - apps/dsa-desktop/package.json
    - apps/dsa-desktop/package-lock.json
    - scripts/build-backend-macos.sh
    - docker/Dockerfile
    - docker/docker-compose.yml
---

## 1. 使用的系统与工具

仓库是一个多语言项目，包含 Python 后端、React Web 前端、Electron 桌面端和 Bot，因此依赖管理按语言分治：

- **Python（后端 / bot / data_provider / src）**：使用 `requirements.txt` 作为唯一依赖清单，通过 `pip install -r requirements.txt` 安装；CI 通过 `.github/requirements-ci.txt` 引用主清单并追加 flake8、pytest。
- **Web 前端（apps/dsa-web）**：使用 `package.json` + `package-lock.json`（npm）锁定版本，构建脚本为 `vite build`。
- **桌面端（apps/dsa-desktop）**：使用独立的 `package.json` + `package-lock.json`，基于 Electron + electron-builder 打包，发布到 GitHub Releases 进行自动更新（electron-updater）。
- **Docker**：`docker/Dockerfile` 与 `docker/docker-compose.yml` 用于容器化部署，依赖由镜像内 pip/npm 安装。

没有发现 Poetry、Pipenv、uv、conda 等替代工具的使用痕迹，也没有 vendoring（如 `vendor/` 目录），所有第三方包均通过远程包管理器拉取。

## 2. 关键文件

- `requirements.txt`：后端全部 Python 依赖的单一来源，包含 FastAPI、litellm、ccxt、pandas、scikit-learn、discord.py、jinja2 等。
- `pyproject.toml`：仅配置 black、isort、bandit 等代码质量工具，**不声明 Python 运行时依赖**。
- `setup.cfg`：集中 flake8、pytest、isort 配置，定义测试标记（unit/integration/network/legacy_stock）。
- `.github/requirements-ci.txt`：在 CI 中 `-r ../requirements.txt` 复用主清单，再追加 flake8、pytest。
- `apps/dsa-web/package.json` + `apps/dsa-web/package-lock.json`：Web 前端依赖及精确锁定。
- `apps/dsa-desktop/package.json` + `apps/dsa-desktop/package-lock.json`：Electron 桌面端依赖及打包配置（GitHub Releases 发布）。
- `scripts/build-backend-macos.sh`：构建脚本中显式执行 `python -m pip install -r requirements.txt` 和 pyinstaller 打包。
- `docker/Dockerfile`：容器镜像构建入口。

## 3. 架构与约定

### Python 依赖策略
- **单文件清单**：所有 Python 依赖集中在根目录 `requirements.txt`，无子模块独立 `requirements*.txt`。
- **宽松下限 + 上限保护**：大多数包使用 `>=X.Y.Z` 形式声明最低兼容版本，部分对稳定性敏感的包显式加上限，例如：
  - `scikit-learn>=1.4.0,<2.0.0` — 避免未来大版本破坏。
  - `tiktoken>=0.8.0,<0.12.0` — 注释说明 pin <0.12 是为避免插件注册问题（issue #537）。
  - `websockets>=12.0,<16.0` — 注释说明使用 legacy client 以避免 asyncio handshake 噪音。
  - `litellm>=1.80.10,!=1.82.7,!=1.82.8,<2.0.0` — 排除已知坏构建版本并限制 major 升级。
- **注释驱动的版本决策**：每个依赖行附带一行中文注释说明用途或版本约束原因，便于后续维护者理解为何如此 pin。
- **可选/条件依赖**：通过 `httpx[socks]` 等 extras 方式声明可选功能；SQLite 被明确标注为内置无需额外依赖。
- **无虚拟环境锁定**：未发现 `requirements.lock`、`poetry.lock` 或 `Pipfile.lock` 形式的 Python 锁文件；依赖版本由 `requirements.txt` 中的范围决定，实际解析结果位于本地 `.venv/`。
- **CI 复用主清单**：`.github/requirements-ci.txt` 通过 `-r ../requirements.txt` 复用，保证 CI 环境与开发环境一致。

### Web 前端依赖策略
- 使用 npm 的 `package.json` 声明依赖，`package-lock.json` 锁定精确版本。
- 通过 `engines.node >=20.19.0 <27` 约束 Node 版本范围。
- 依赖分为 `dependencies`（axios、react、recharts、zustand 等）与 `devDependencies`（vite、vitest、eslint、playwright 等）两类。

### 桌面端依赖策略
- 独立 `package.json`，依赖 `electron`、`electron-builder`、`electron-updater`。
- 通过 electron-builder 将后端可执行文件（`../../dist/backend/stock_analysis`）打包进应用，并发布到 GitHub Releases 供客户端自动更新。

### Docker 部署
- `docker/Dockerfile` 是容器化入口，结合 `docker/docker-compose.yml` 编排服务。
- 未使用私有 registry 或特殊 pip index 配置，默认从 PyPI/npm 官方源拉取。

## 4. 约定与约束

- **Python 依赖必须写在根 `requirements.txt`**：新增后端依赖需在此文件添加，并遵循“带注释说明用途”的约定。
- **敏感/不稳定依赖需加版本上限**：对于可能引入破坏性变更的库（如 scikit-learn、tiktoken、websockets、litellm），应显式添加 `<major` 上限并在注释中说明原因。
- **CI 环境通过 `-r requirements.txt` 复用**：不得在 CI 单独维护一套依赖列表，避免漂移。
- **前端依赖通过 npm lockfile 锁定**：修改 `package.json` 后需提交对应的 `package-lock.json`。
- **桌面端发布流程绑定 GitHub Releases**：electron-builder 配置了 `provider: github`，更新包通过 GitHub Releases 分发。
- **无 vendoring、无私有 registry**：所有第三方包均来自公开 PyPI/npm 源；若需接入私有源，需在 pip/npm 层面另行配置（当前未见相关设置）。
- **测试依赖与运行依赖分离**：flake8、pytest 仅在 CI 清单中追加，不混入主 `requirements.txt`。