---
kind: error_handling
name: FastAPI 全局异常中间件 + LLM 参数错误恢复 + 前端分类化错误处理
category: error_handling
scope:
    - '**'
source_files:
    - api/middlewares/error_handler.py
    - api/v1/errors.py
    - api/app.py
    - src/llm/errors.py
    - apps/dsa-web/src/api/error.ts
    - bot/models.py
    - bot/handler.py
---

## 1. 整体方案

本项目采用 **分层错误处理**：
- API 层（FastAPI）通过 `api/middlewares/error_handler.py` 注册全局异常处理器，统一返回 `{error, message, detail}` 结构；
- 业务/服务层抛出 Python 异常或返回结构化错误字典，由 FastAPI 的 `HTTPException` / `RequestValidationError` 处理器归一化；
- LLM 调用层在 `src/llm/errors.py` 中实现 LiteLLM 参数错误的自动分类与一次重试恢复；
- Web 前端在 `apps/dsa-web/src/api/error.ts` 中将后端错误解析为带 `category` 的 `ParsedApiError`，用于 UI 提示。

## 2. 关键文件与职责

| 文件 | 职责 |
|---|---|
| `api/middlewares/error_handler.py` | 定义 `ErrorHandlerMiddleware`（Starlette `BaseHTTPMiddleware`）捕获未处理异常；提供 `add_error_handlers(app)` 注册 `HTTPException`、`RequestValidationError`、通用 `Exception` 三个处理器 |
| `api/v1/errors.py` | 工具函数 `error_body`、`api_error`、`error_json_response`，构造统一的 `{error, message, detail}` 响应体 |
| `api/app.py` | 应用工厂，在创建 FastAPI 实例后调用 `add_error_handlers(app)` 注册全局处理器 |
| `src/llm/errors.py` | `classify_litellm_generation_param_error` 将 LiteLLM 报错文本归类为 `GenerationParamRecovery`；`call_litellm_with_param_recovery` 包装一次失败后的参数恢复重试 |
| `apps/dsa-web/src/api/error.ts` | 前端错误解析器：从 Axios 错误中提取状态码、payload、消息，按关键词匹配分类为 `agent_disabled`、`missing_params`、`llm_not_configured`、`model_tool_incompatible`、`invalid_tool_call`、`portfolio_oversell`、`portfolio_busy`、`upstream_llm_400`、`upstream_timeout`、`upstream_network`、`local_connection_failed`、`http_error`、`unknown` |
| `bot/models.py` | Bot 侧 `WebhookResponse.success/error/challenge` 与 `BotResponse.error_response`，作为平台 webhook 的统一错误载体 |
| `bot/handler.py` | Webhook 入口，对 JSON 解析失败等直接返回 `WebhookResponse.error(..., 400)`，命令执行异常仅记录日志不向上抛 |

## 3. 架构与约定

### 3.1 FastAPI 全局异常处理
- `add_error_handlers` 注册了三个处理器：
  - `HTTPException`：若 `exc.detail` 已是 `{error, message}` 格式则原样返回，否则包装为 `{error: "http_error", message, detail: None}`；
  - `RequestValidationError`：返回 422，`detail` 为 Pydantic 验证错误列表；
  - 通用 `Exception`：记录堆栈并返回 500 `{error: "internal_error", message: "服务器内部错误"}`，仅在 DEBUG 级别附带 `detail`。
- `ErrorHandlerMiddleware` 是额外的 Starlette 中间件，同样捕获未处理异常并以相同 500 结构返回，形成双重兜底。
- 所有业务端点应优先使用 `api.v1.errors.api_error()` / `error_json_response()` 返回业务错误，而不是裸 `raise HTTPException`。

### 3.2 LLM 参数错误恢复
- `classify_litellm_generation_param_error` 基于正则匹配错误文本中的 `temperature`、`top_p`、`presence_penalty`、`frequency_penalty`、`seed` 等字段以及 `unsupported`、`not supported`、`unrecognized`、`only default temperature value is ...` 等标记，返回 `GenerationParamRecovery`（设置/省略参数及原因）。
- `call_litellm_with_param_recovery` 先正常调用 LiteLLM，失败时尝试一次恢复重试；若恢复成功且开启缓存，会通过 `remember_litellm_generation_param_recovery` 记住该模型的恢复策略，避免后续重复失败。

### 3.3 前端错误分类
- `parseApiError` 将任意 Axios 错误转换为 `ParsedApiError`，通过拼接 `rawMessage`、`errorMessage`、`causeMessage`、`errorCode`、`response.statusText` 构建匹配文本，再按关键词优先级匹配具体类别。
- 分类结果携带 `status`、`category`、`title`、`message`、`rawMessage`，供 UI 组件显示友好提示。
- 常见分类包括：Agent 未启用、缺少必要参数、LLM 未配置、模型不支持工具调用、上游 400、超时、网络不可达、本地连接失败等。

### 3.4 Bot/Webhook 错误
- `handle_webhook` / `handle_webhook_async` 对未知平台、JSON 解析失败直接返回 `WebhookResponse.error(..., 400)`；
- 命令执行异常仅 `logger.error` 记录，不向平台返回错误，避免用户收到无意义的错误消息；
- `BotResponse.error_response` 以 `❌ 错误：...` 前缀返回给用户。

## 4. 约定与约束

- **API 响应体约定**：所有业务错误必须遵循 `{error: string, message: string, detail?: any}` 结构，由 `api/v1/errors.py` 的工具函数生成，确保前后端一致。
- **未处理异常兜底**：任何未被显式处理的异常都会落入 `add_error_handlers` 注册的通用处理器，返回 500 且 `error="internal_error"`，同时记录完整堆栈到日志。
- **Pydantic 校验错误**：由 FastAPI 的 `RequestValidationError` 处理器统一转为 422，`detail` 字段为验证错误数组，前端通过 `extractValidationDetail` 提取可读信息。
- **LiteLLM 错误不直接暴露给上层**：通过 `call_litellm_with_param_recovery` 拦截并重试一次，只有无法恢复的错误才会继续向上抛出。
- **前端不直接展示原始错误**：所有错误必须经 `parseApiError` 分类后再呈现，保证用户看到的是中文、可操作的提示而非技术细节。
- **Bot Webhook 不抛异常**：Webhook 入口只返回 `WebhookResponse`，异常通过日志记录，避免第三方平台反复重试导致风暴。

## 5. 适用性说明

本仓库是一个包含 FastAPI 后端、React Web 前端、多平台 Bot 的全栈项目，错误处理贯穿 API 网关、LLM 调用链路与前端 UI 三层，具有明确的中间件、分类器和恢复机制，因此本类别完全适用。