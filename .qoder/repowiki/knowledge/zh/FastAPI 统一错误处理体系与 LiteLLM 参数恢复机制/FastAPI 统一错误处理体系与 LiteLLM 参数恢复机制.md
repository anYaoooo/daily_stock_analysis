---
kind: error_handling
name: FastAPI 统一错误处理体系与 LiteLLM 参数恢复机制
category: error_handling
scope:
    - '**'
source_files:
    - api/middlewares/error_handler.py
    - api/v1/errors.py
    - api/app.py
    - src/llm/errors.py
    - src/logging_config.py
    - api/v1/endpoints/backtest.py
    - api/v1/endpoints/agent.py
    - api/v1/endpoints/alerts.py
---

## 1. 系统/方法概述
本仓库采用 FastAPI + Starlette 作为 API 框架，通过「中间件 + 全局异常处理器 + 端点级 HTTPException」三层结构实现统一的错误定义、传播与响应格式化；在 LLM 调用层（LiteLLM）额外提供基于错误文本分类的「一次重试 + 参数自动恢复」机制，以应对不同模型对 generation parameters 的支持差异。

## 2. 关键文件与包
- `api/middlewares/error_handler.py`：全局异常处理中间件与 FastAPI 异常处理器注册（HTTPException、RequestValidationError、通用 Exception）
- `api/v1/errors.py`：统一的错误体构造器 `error_body`、`api_error`、`error_json_response`
- `api/app.py`：应用工厂，集中注册 CORS、认证中间件、v1 路由以及 `add_error_handlers(app)`
- `src/llm/errors.py`：LiteLLM 生成参数错误分类与 `call_litellm_with_param_recovery` 重试封装
- `src/logging_config.py`：统一日志配置（控制台 + 常规日志文件 + 调试日志文件），为错误追踪提供基础
- `api/v1/endpoints/*.py`：各业务端点中直接 raise HTTPException 的错误使用示例（如 backtest.py、agent.py、alerts.py）

## 3. 架构与约定
### 3.1 全局异常处理中间件
- `ErrorHandlerMiddleware` 继承 `BaseHTTPMiddleware`，在 `dispatch` 中 try/catch 所有未捕获异常，记录完整堆栈并返回固定格式的 500 JSON 响应。
- `add_error_handlers(app)` 为 FastAPI 注册三类异常处理器：
  - `HTTPException`：若 detail 已是 `{error, message}` 字典则原样返回，否则包装成标准格式。
  - `RequestValidationError`：422 响应，detail 携带 Pydantic 验证错误列表。
  - `Exception`：兜底 500 响应，仅记录日志，不暴露内部细节给客户端。

### 3.2 统一错误体结构
所有 API 错误响应遵循一致的 JSON 结构：
```json
{
  "error": "错误码标识",      // 如 internal_error / validation_error / http_error / invalid_params / not_found
  "message": "人类可读消息",   // 中文描述
  "detail": null|any          // 可选，包含详细错误信息或验证错误列表
}
```
由 `error_body()` 构造，`api_error()` 和 `error_json_response()` 分别用于快速抛出 HTTPException 或直接返回 JSONResponse。

### 3.3 端点级错误模式
各业务端点（如 `backtest.py`、`agent.py`、`alerts.py`）普遍采用以下模式：
- 参数校验失败 → `raise HTTPException(status_code=400, detail={"error": "invalid_params", "message": "..."})`
- 业务异常 → try/except 捕获后记录日志并 `raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "..."})`
- 资源不存在 → `raise HTTPException(status_code=404, detail={"error": "not_found", "message": "..."})`

### 3.4 LiteLLM 参数恢复机制
`src/llm/errors.py` 实现了针对 LiteLLM 生成参数的智能恢复：
- `classify_litellm_generation_param_error()` 解析错误文本，识别不支持的参数（如 temperature、top_p、presence_penalty、frequency_penalty、seed）
- 支持从错误消息中提取「仅允许默认温度值」等约束，自动调整请求参数
- `call_litellm_with_param_recovery()` 封装一次调用 + 一次自动重试，成功后缓存恢复策略避免重复失败

### 3.5 日志与错误追踪
- 统一通过 `src/logging_config.setup_logging()` 初始化，输出到控制台、INFO 级别日志文件和 DEBUG 级别日志文件
- 错误中间件记录请求路径、方法、完整堆栈，便于问题定位
- LiteLLM 相关 logger 单独控制日志级别，避免噪声

## 4. 约定与约束
- **错误响应格式必须一致**：所有 HTTP 错误响应必须包含 `error`、`message` 字段，可选 `detail` 字段（由 `error_body()` 保证）
- **禁止裸抛异常**：业务代码应通过 `HTTPException` 明确状态码和错误语义，而非抛出 Python 原生异常
- **验证错误统一走 Pydantic**：请求参数验证失败由 FastAPI 自动处理，返回 422 及详细验证错误
- **未捕获异常兜底**：所有未处理的异常最终被全局中间件捕获，返回 500 且仅记录日志，不泄露内部细节
- **LiteLLM 调用必须使用恢复封装**：涉及 LLM 调用的代码应使用 `call_litellm_with_param_recovery()` 而非直接调用，以获得参数自动恢复能力
- **前端静态资源错误特殊处理**：`/assets/*` 路由对缺失资源返回 text/plain 404，避免浏览器因收到 JSON 而静默失败