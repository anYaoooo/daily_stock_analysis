---
kind: logging_system
name: 基于 Python logging 的三层日志系统（控制台/常规文件/调试文件）
category: logging_system
scope:
    - '**'
source_files:
    - src/logging_config.py
    - server.py
    - main.py
    - webui.py
    - tests/test_logging_config.py
---

## 1. 使用的框架与工具

- 标准库 `logging`，未引入第三方日志框架（如 loguru、structlog）。
- 通过 `logging.handlers.RotatingFileHandler` 实现按大小轮转的文件日志。
- 自定义 `RelativePathFormatter`，将日志中的 `pathname` 从绝对路径转换为相对于项目根目录的相对路径。

## 2. 核心文件

- `src/logging_config.py`：唯一集中式日志初始化模块，提供 `setup_logging()` 入口。
- `server.py`：FastAPI 后端启动入口，调用 `setup_logging(log_prefix="api_server", ...)`。
- `main.py`：CLI/调度主程序，先通过 `_setup_bootstrap_logging()` 输出到 stderr，再在配置加载后调用 `_setup_runtime_logging()` → `setup_logging(log_prefix="stock_analysis", ...)`。
- `webui.py`：WebUI 独立入口，使用 `log_prefix="web_server"`。
- `tests/test_logging_config.py`：对日志格式、LiteLLM 日志级别降级、无效环境变量回退等行为进行回归测试。

## 3. 架构与约定

### 3.1 统一的初始化入口
所有进程级入口统一通过 `src.logging_config.setup_logging()` 完成日志系统初始化。该函数负责：
- 创建 `./logs` 目录。
- 设置根 logger 级别为 `DEBUG`，由各 handler 控制实际输出级别。
- 清除已有 handlers，避免重复添加。
- 使用 `RelativePathFormatter(LOG_FORMAT, LOG_DATE_FORMAT, relative_to=project_root)` 格式化输出。

### 3.2 三层输出策略
| 通道 | Handler | 默认级别 | 轮转策略 | 文件名模式 |
|---|---|---:|---|---|
| 控制台 | `StreamHandler(sys.stdout)` | `debug=True` 时 DEBUG，否则 INFO | 无 | — |
| 常规日志文件 | `RotatingFileHandler` | INFO | 10MB，保留 5 份 | `{prefix}_{YYYYMMDD}.log` |
| 调试日志文件 | `RotatingFileHandler` | DEBUG | 50MB，保留 3 份 | `{prefix}_debug_{YYYYMMDD}.log` |

三个入口使用的 `log_prefix` 不同，从而区分进程：`stock_analysis`（CLI/调度）、`api_server`（FastAPI）、`web_server`（WebUI）。

### 3.3 日志格式
```python
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(pathname)s:%(lineno)d | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
```
每条日志包含时间、级别、logger 名称、相对路径+行号、消息体，便于定位来源。

### 3.4 第三方库降噪
默认将以下库的日志级别降至 WARNING：`urllib3`、`sqlalchemy`、`google`、`httpx`。可通过 `extra_quiet_loggers` 参数追加（例如 API 启动时传入 `['uvicorn', 'fastapi']`）。

### 3.5 LiteLLM 日志级别控制
LiteLLM 相关 logger（`LiteLLM`、`LiteLLM Router`、`LiteLLM Proxy`、`litellm`）的级别由环境变量 `LITELLM_LOG_LEVEL` 控制；若为空或无效则回退为 `WARNING`，并记录一条 warning 提示可选值。测试覆盖了空值、无效值、DEBUG 三种场景。

### 3.6 启动期日志降级
`main.py` 中 `_setup_bootstrap_logging()` 在配置尚未加载前仅向 `sys.stderr` 输出简单格式的日志，避免在未确定 `log_dir` 时创建文件；随后 `_setup_runtime_logging()` 再切换到完整的文件+控制台输出，并在文件 I/O 失败时降级回控制台。

## 4. 使用约定与约束

- **每个进程入口必须调用 `setup_logging()`**：`server.py`、`main.py`、`webui.py` 均显式调用，确保根 logger 被正确配置。
- **业务代码通过 `logging.getLogger(__name__)` 获取 logger**：所有 API endpoint、中间件、服务模块均采用此方式，不直接操作根 logger。
- **日志级别使用规范**：
  - 常规运行输出 INFO 及以上到常规日志文件。
  - 详细诊断信息（含请求参数、内部状态）写入 DEBUG 级别的 debug 日志文件。
  - 警告和错误通过 `logger.warning` / `logger.error` 输出，同时进入常规日志。
- **日志文件命名约定**：`{进程前缀}_{日期}.log` 与 `{进程前缀}_debug_{日期}.log`，由 `datetime.now().strftime('%Y%m%d')` 生成，便于按天归档。
- **路径显示约定**：通过 `RelativePathFormatter` 强制输出相对路径，避免容器/CI 环境中绝对路径泄露。
- **可观测性字段**：日志中包含 `%(name)s`（模块名）和 `%(lineno)d`（行号），配合 `%(pathname)s` 可在 IDE 中直接跳转。
- **Docker 部署注意**：`main.py` 中对文件日志初始化失败的异常捕获会降级为控制台输出，并提示 Docker 用户检查挂载目录权限。

## 5. 覆盖范围

当前仓库中所有 Python 模块（`api/*`、`src/*`、`bot/*`、`scripts/*`、`tests/*`）均通过标准 `logging` 模块输出日志，并由上述 `setup_logging()` 统一配置。未发现其他独立的日志子系统或结构化日志框架。