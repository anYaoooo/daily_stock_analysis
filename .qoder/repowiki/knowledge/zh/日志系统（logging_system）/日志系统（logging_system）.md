---
kind: logging_system
name: 日志系统（logging_system）
category: logging_system
scope:
    - '**'
source_files:
    - src/logging_config.py
    - main.py
    - server.py
    - webui.py
---

## 1. 使用的系统与框架
- 基于 Python 标准库 `logging`，配合 `logging.handlers.RotatingFileHandler` 实现文件轮转。
- 未引入第三方日志框架（如 loguru、structlog），所有日志配置与输出均通过内置模块完成。

## 2. 核心文件与入口
- `src/logging_config.py`：统一的日志初始化模块，提供 `setup_logging()` 以及自定义 Formatter、默认静默 logger 列表等。
- `main.py`：CLI 主入口，定义 `_setup_bootstrap_logging()`（启动前 stderr-only）和 `_setup_runtime_logging()`（加载配置后切换到正式 handler），并调用 `setup_logging(log_prefix="stock_analysis", ...)`。
- `server.py`：FastAPI 后端启动脚本，通过 `setup_logging(log_prefix="api_server", console_level=..., extra_quiet_loggers=['uvicorn', 'fastapi'])` 初始化 API 进程日志。
- `webui.py`：WebUI 启动脚本，使用 `setup_logging(log_prefix="web_server")` 初始化 Web 服务日志。
- `logs/`：运行时日志目录，包含按日期分文件的常规日志（`*_YYYYMMDD.log`）与调试日志（`*_debug_YYYYMMDD.log`）。

## 3. 架构与约定
- **统一初始化点**：所有进程在启动早期调用 `src.logging_config.setup_logging()`，由该函数负责创建根 logger、添加控制台与文件 handlers、降低第三方库日志级别，并输出初始化信息。
- **三层输出**：
  - 控制台（StreamHandler to stdout）：级别由 `debug` 参数或 `console_level` 决定；CLI 启动前还会先向 stderr 输出一个临时 formatter，避免配置文件尚未就绪时丢失关键信息。
  - 常规日志文件（RotatingFileHandler）：INFO 级别，单文件最大 10MB，保留 5 个备份。
  - 调试日志文件（RotatingFileHandler）：DEBUG 级别，单文件最大 50MB，保留 3 个备份。
- **日志格式**：`%(asctime)s | %(levelname)-8s | %(name)s | %(pathname)s:%(lineno)d | %(message)s`，时间格式 `%Y-%m-%d %H:%M:%S`。
- **相对路径格式化**：通过 `RelativePathFormatter` 将绝对路径转换为相对于项目根目录的相对路径，便于容器化与跨环境查看。
- **第三方库降噪**：默认对 `urllib3`、`sqlalchemy`、`google`、`httpx` 等设置 WARNING 级别；LiteLLM 相关 logger 级别可通过环境变量 `LITELLM_LOG_LEVEL` 控制，无效值会回退到默认 WARNING 并记录警告。
- **进程级隔离**：不同进程使用不同的 `log_prefix`（`stock_analysis`、`api_server`、`web_server`），生成独立日志文件，避免相互覆盖。
- **降级策略**：`_setup_runtime_logging()` 在文件 I/O 失败时捕获异常并降级为仅控制台输出，同时记录警告说明可能的权限/挂载问题。

## 4. 约定与约束
- **日志级别使用**：业务代码通过 `logger = logging.getLogger(__name__)` 获取模块级 logger，遵循 INFO/WARNING/ERROR 分级；调试信息使用 DEBUG。
- **文件命名约定**：日志文件按 `log_prefix_YYYYMMDD.log` 与 `log_prefix_debug_YYYYMMDD.log` 命名，存放于 `./logs` 目录。
- **环境变量控制**：
  - `LITELLM_LOG_LEVEL`：控制 LiteLLM 相关 logger 的级别，支持 DEBUG/INFO/WARNING/ERROR/CRITICAL，不合法值自动回退并告警。
  - CLI 启动参数 `--debug`：影响控制台与调试日志级别。
- **测试覆盖**：`tests/test_logging_config.py` 对 `setup_logging` 的行为进行断言，确保轮转、级别、静默 logger 等行为符合预期。
- **Docker 集成**：`docker/entrypoint.sh` 会在容器启动时修复默认日志目录权限，避免文件写入失败导致降级为仅控制台输出。

总体来看，该项目的日志系统以标准库 `logging` 为核心，通过 `src/logging_config.py` 提供统一初始化能力，并在 CLI、API、WebUI 三个进程入口中分别配置不同前缀与级别，形成清晰、可维护且具备降级能力的日志体系。