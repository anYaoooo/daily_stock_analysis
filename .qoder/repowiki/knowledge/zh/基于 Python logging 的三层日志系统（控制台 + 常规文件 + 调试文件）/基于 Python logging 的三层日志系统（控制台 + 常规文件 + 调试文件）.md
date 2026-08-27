---
kind: logging_system
name: 基于 Python logging 的三层日志系统（控制台 + 常规文件 + 调试文件）
category: logging_system
scope:
    - '**'
source_files:
    - src/logging_config.py
    - server.py
    - main.py
    - tests/test_logging_config.py
---

## 1. 使用的框架与工具

- 使用 Python 标准库 `logging`，未引入第三方日志框架。
- 通过 `logging.handlers.RotatingFileHandler` 实现按大小轮转的文件输出。
- 自定义 `RelativePathFormatter` 将日志中的绝对路径转换为相对项目根目录的路径，便于跨机器/容器共享日志。

## 2. 核心文件

- `src/logging_config.py`：统一的日志初始化入口 `setup_logging()`，定义格式、级别常量、第三方库降噪策略。
- `server.py`：FastAPI 服务启动时调用 `setup_logging(log_prefix="api_server", ...)`，并额外把 `uvicorn`、`fastapi` 加入静默列表。
- `main.py`：CLI / 调度器入口，先通过 `_setup_bootstrap_logging()` 在配置加载前写 stderr，再调用 `_setup_runtime_logging()` 切换到正式日志；同时负责捕获日志目录不可写时的降级逻辑。
- `tests/test_logging_config.py`：对日志格式、LiteLLM 级别、无效级别回退等行为进行回归测试。

## 3. 架构与约定

### 3.1 统一初始化入口
所有模块不应直接配置 root logger，而是通过 `src.logging_config.setup_logging` 完成。该函数会：
- 设置根 logger 级别为 `DEBUG`，由具体 handler 控制实际输出级别。
- 清除已有 handler，避免重复添加。
- 创建三个 sink：
  1. **控制台**：`StreamHandler(sys.stdout)`，级别由 `debug=True` → DEBUG，否则 INFO。
  2. **常规日志文件**：`RotatingFileHandler("logs/{prefix}_{YYYYMMDD}.log")`，INFO 级别，单文件最大 10MB，保留 5 份备份。
  3. **调试日志文件**：`RotatingFileHandler("logs/{prefix}_debug_{YYYYMMDD}.log")`，DEBUG 级别，单文件最大 50MB，保留 3 份备份。
- 自动降低第三方库日志级别：默认把 `urllib3`、`sqlalchemy`、`google`、`httpx` 降到 WARNING；LiteLLM 相关 logger（`LiteLLM`、`LiteLLM Router`、`LiteLLM Proxy`、`litellm`）级别由环境变量 `LITELLM_LOG_LEVEL` 决定，默认 WARNING，非法值会记录警告并回退到 WARNING。

### 3.2 日志格式
统一格式字符串：
```
%(asctime)s | %(levelname)-8s | %(name)s | %(pathname)s:%(lineno)d | %(message)s
```
其中 `%(pathname)s` 经 `RelativePathFormatter` 处理后是相对于 `Path.cwd()` 的相对路径，日期格式为 `%Y-%m-%d %H:%M:%S`。

### 3.3 启动阶段的两段式日志
`main.py` 采用两段式初始化：
1. `_setup_bootstrap_logging()`：在 `config.log_dir` 未知之前，仅向 `sys.stderr` 写入简单格式的日志，确保早期错误可被观察到。
2. `_setup_runtime_logging()`：读取配置后调用 `setup_logging(log_prefix="stock_analysis", log_dir=config.log_dir, debug=...)`；若日志目录不可写（OSError），则降级为仅控制台输出，并记录警告。

### 3.4 不同进程的日志前缀
- CLI / 定时任务进程：`log_prefix="stock_analysis"`，生成 `stock_analysis_YYYYMMDD.log` 和 `stock_analysis_debug_YYYYMMDD.log`。
- API 服务进程：`log_prefix="api_server"`，生成 `api_server_YYYYMMDD.log` 和 `api_server_debug_YYYYMMDD.log`。
- 其他进程可通过传入 `log_prefix` 区分来源。

### 3.5 日志级别策略
- 根 logger 始终设为 `DEBUG`，由 handler 过滤。
- 控制台级别受 `--debug` 或 `console_level` 参数控制。
- 常规文件固定 INFO，调试文件固定 DEBUG。
- 第三方库默认降为 WARNING，LiteLLM 可通过 `LITELLM_LOG_LEVEL` 覆盖。
- FastAPI 启动时额外把 `uvicorn`、`fastapi` 加入静默列表，减少请求级噪音。

## 4. 约定与约束

- **禁止直接 `print` 或裸 `logging.info` 绕过此模块**：所有新模块应通过 `import logging; logger = logging.getLogger(__name__)` 并使用已配置的 root logger。
- **日志目录必须可写**：`setup_logging` 会自动 `mkdir(parents=True, exist_ok=True)`，但 `main.py` 中仍包含 OSError 降级逻辑，用于 Docker 只读挂载等场景。
- **文件名按日期分片**：文件名模式 `{prefix}_{date}.log`，由 `datetime.now().strftime('%Y%m%d')` 生成，不存在按大小自动重命名的“滚动”机制，轮转由 `RotatingFileHandler` 按字节数触发。
- **LiteLLM 日志级别由环境变量驱动**：`LITELLM_LOG_LEVEL` 支持 `DEBUG/INFO/WARNING/ERROR/CRITICAL`，空值或空白视为默认 WARNING，非法值会记录一条 warning 并回退。
- **路径相对化**：`RelativePathFormatter` 会把绝对路径转为相对 `cwd()` 的路径，若无法转换则保持原样，因此日志中不会出现宿主机绝对路径（除非 cwd 不在项目内）。
- **测试保障**：`tests/test_logging_config.py` 断言日志格式包含 logger name、LiteLLM 默认安静、无效级别回退等行为，属于强制验证点。

## 5. 当前局限

- 日志格式为纯文本，非 JSON 结构化格式，不便于直接进 ELK/ Loki 等结构化日志系统解析。
- 没有集中化的 trace/correlation id 注入（如每个请求或每次分析任务带唯一 ID），仅在部分业务逻辑中自行拼接上下文字段。
- 日志轮转仅按文件大小，不按时间切割；每日新文件由调用方在每次 `setup_logging` 时重新指定文件名实现。
