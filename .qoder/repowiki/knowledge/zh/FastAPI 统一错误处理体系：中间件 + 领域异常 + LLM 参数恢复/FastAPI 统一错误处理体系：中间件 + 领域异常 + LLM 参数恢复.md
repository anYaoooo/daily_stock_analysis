---
kind: error_handling
name: FastAPI 统一错误处理体系：中间件 + 领域异常 + LLM 参数恢复
category: error_handling
scope:
    - '**'
source_files:
    - api/v1/errors.py
    - api/middlewares/error_handler.py
    - api/app.py
    - src/llm/errors.py
    - api/v1/endpoints/analysis.py
    - api/v1/endpoints/portfolio.py
    - api/v1/endpoints/health.py
---

## 1. 整体方案

本项目基于 FastAPI/Starlette，采用「全局异常处理器 + 中间件兜底 + 领域专用错误构造器」的分层策略：
- API 层通过 `api/v1/errors.py` 中的 `api_error()` / `error_json_response()` 构造统一的 `{error, message, detail}` JSON 响应；
- `api/middlewares/error_handler.py` 注册三个全局异常处理器（`HTTPException`、`RequestValidationError`、通用 `Exception`），并附带一个 `BaseHTTPMiddleware` 作为最后兜底；
- LLM 调用层在 `src/llm/errors.py` 中实现 LiteLLM 生成参数的错误分类与一次自动重试恢复；
- 业务层（`src/services/*`、`bot/*`、`data_provider/*`）主要抛出 Python 原生异常，由上层捕获后转为 HTTP 错误或记录日志。

## 2. 关键文件与职责

| 文件 | 职责 |
|---|---|
| `api/v1/errors.py` | 定义 `error_body`、`api_error`、`error_json_response`，统一返回体结构 `{error, message, detail?}` |
| `api/middlewares/error_handler.py` | `ErrorHandlerMiddleware` 兜底未处理异常；`add_error_handlers(app)` 注册 `HTTPException` / `RequestValidationError` / `Exception` 处理器 |
| `api/app.py` | 应用工厂，调用 `add_error_handlers(app)` 完成注册，CORS 之后、路由之前 |
| `src/llm/errors.py` | 解析 LiteLLM 报错文本，识别不支持的 temperature/top_p/frequency_penalty/seed 等参数，返回 `GenerationParamRecovery` 并调用 `call_litellm_with_param_recovery` 执行一次带修正的重试 |
| `api/v1/endpoints/*.py` | 各端点通过 `raise api_error(status, code, msg, detail=...)` 抛出业务错误 |

## 3. 架构与约定

### 3.1 统一响应体结构
所有 API 错误响应遵循固定三字段结构：
```json
{
  "error": "业务错误码",
  "message": "人类可读消息",
  "detail": "可选的原始细节"
}
```
`api.v1.errors.error_body()` 负责组装，`api_error()` 将其放入 `HTTPException.detail`，再由全局处理器原样返回。

### 3.2 全局异常处理器优先级
`add_error_handlers` 注册的顺序决定匹配优先级：
1. `HTTPException`：若 `exc.detail` 已是 `{error, message}` 格式则直接透传，否则包装为 `{error: "http_error", ...}`；
2. `RequestValidationError`：422，`error="validation_error"`，`detail` 为 Pydantic 验证错误列表；
3. 通用 `Exception`：500，`error="internal_error"`，`message="服务器内部错误"`，`detail=None`；
4. `ErrorHandlerMiddleware.dispatch` 再捕获一次未被任何处理器接住的异常，同样返回 500，但 `detail` 仅在 DEBUG 级别开启时包含堆栈。

### 3.3 业务端点抛错约定
各 endpoint 统一使用 `raise api_error(status_code, error_code, message, detail=...)`，例如：
- 400 `validation_error`：缺少必填参数、BTC 代码为空等；
- 404 `not_found`：任务不存在、账户不存在等；
- 409 `conflict`：重复操作；
- 500 `internal_error` / `analysis_failed`：分析失败、查询失败等。

### 3.4 LLM 参数错误恢复
`src/llm/errors.py` 对 LiteLLM 抛出的异常进行文本级分类：当错误信息包含 `unsupported` / `not supported` / `unrecognized` / `unknown parameter` / `not allowed` / `invalid parameter` / `does not support` 等标记时，识别出被拒绝的生成参数（temperature、top_p、presence_penalty、frequency_penalty、seed），并通过 `apply_litellm_generation_param_recovery` 移除或替换该参数后重试一次。若仍失败则向上抛出，由上层捕获。

### 3.5 前端资源 404 的特殊处理
`api/app.py` 中对 `/assets/{path}` 单独处理缺失资源，返回 `text/plain` 的 404，避免默认 JSON 错误掩盖前端打包不一致问题（见注释引用 GitHub #1064/#1065）。SPA 回退路由也对非 API 路径返回 index.html。

## 4. 约定与约束

- **禁止直接返回裸字符串或 dict**：所有 API 错误必须经 `api_error()` / `error_json_response()` 包装，保证 `error/message/detail` 三元组一致。
- **HTTP 异常优先于 raise 普通 Exception**：业务可预见的错误应构造 `HTTPException`（通过 `api_error`），让全局处理器以正确状态码返回；仅真正不可预期的崩溃才依赖中间件兜底。
- **验证错误走 Pydantic**：请求体验证失败由 FastAPI 的 `RequestValidationError` 统一处理，不手动 raise 422。
- **LLM 调用必须包裹 `call_litellm_with_param_recovery`**：确保 provider 拒绝某生成参数时能自动降级重试一次，避免上层感知具体 provider 差异。
- **调试细节不泄露给客户端**：全局 `Exception` 处理器和中间件兜底的 `detail` 仅在 `logger.isEnabledFor(logging.DEBUG)` 时填充，生产环境只返回 `"服务器内部错误"`。
- **健康检查独立于认证**：`/health` 与 `/api/health` 均暴露且无需认证，供负载均衡器探测。

## 5. 覆盖范围说明

本错误处理体系主要覆盖 FastAPI Web 接口层与 LLM 调用层；CLI、Bot 命令（`bot/commands/*`）、后台定时任务（`scheduler.py`、`services/alert_worker.py` 等）主要通过 `logging` 记录异常，未统一转换为 HTTP 错误。数据获取层（`data_provider/*`）抛出 Python 异常由上层 service 捕获后转业务错误码。