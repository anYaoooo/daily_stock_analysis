---
kind: dependency_management
name: 多语言依赖管理：Python requirements.txt + Node.js package.json 双轨锁定
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
---

本仓库采用多语言、多子项目的依赖管理模式，Python 后端与前端/桌面端各自维护独立的包清单与锁文件，CI 统一通过 requirements.txt 拉取 Python 依赖。

1. Python 后端依赖管理
- 声明式清单：根目录 requirements.txt 按功能分组（核心、飞书、数据处理、AI/LLM、搜索、网络、Discord、网页提取、模板引擎、FastAPI），使用 >= 指定最低版本并辅以 < 上限（如 litellm>=1.80.10,!=1.82.7,!=1.82.8,<2.0.0；tiktoken>=0.8.0,<0.12.0；websockets>=12.0,<16.0）。
- 无 lockfile：未提交 requirements.lock / poetry.lock / Pipfile.lock，安装直接解析语义化版本范围。
- pyproject.toml 仅配置代码风格工具（black、isort、bandit），不声明运行时依赖。
- CI 依赖：.github/requirements-ci.txt 通过 -r ../requirements.txt 引用主清单，并追加 flake8、pytest 作为测试/lint 工具。
- 虚拟环境：根目录存在 .venv，但未被 git 跟踪（.gitignore 排除），本地开发自行创建。

2. Node.js Web 前端依赖管理
- 清单：apps/dsa-web/package.json 使用 ^ 前缀声明依赖（如 react ^19.2.0、axios ^1.13.4），devDependencies 包含 Vite、TypeScript、Vitest、Playwright、TailwindCSS 等。
- 锁文件：apps/dsa-web/package-lock.json（lockfileVersion 3）已提交，确保构建可重现。
- 引擎约束：package.json 中 engines.node>=20.19.0 <27、npm>=10 强制运行环境。

3. Electron 桌面端依赖管理
- 清单：apps/dsa-desktop/package.json 仅声明 electron-updater 为运行时依赖，electron/electron-builder 放在 devDependencies。
- 锁文件：apps/dsa-desktop/package-lock.json 已提交。
- 打包发布：electron-builder 配置 publish.provider=github，自动将安装包发布到 GitHub Releases。

4. CI/CD 中的依赖安装
- 所有 GitHub Actions 工作流（ci.yml、docker-publish.yml、network-smoke.yml、pr-review.yml 等）均通过 pip install -r requirements.txt 安装 Python 依赖。
- pr-review.yml 触发条件包含 requirements.txt、pyproject.toml 变更，PR 审查流程会校验这些文件。

5. 约定与约束
- Python 依赖必须通过 requirements.txt 集中声明，禁止在代码中硬编码版本号或从其他位置引入未声明的包。
- Node 子项目必须提交对应的 package-lock.json，保证依赖树可重现。
- 对上游大版本升级保持谨慎：requirements.txt 中对 litellm、tiktoken、websockets 等关键库显式设置上限或排除已知问题版本。