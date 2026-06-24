# BTC 智能分析系统

基于 AI 的 BTC 交易分析与消息推送工具，聚焦比特币 7x24 行情、CryptoPanic 新闻、ChromaDB 新闻缓存、双向多空策略和通知分发。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| BTC 行情 | 通过 CCXT 对接 Binance 公共行情获取 BTC 实时行情与 K 线数据，无需股票数据源依赖 |
| BTC 新闻 | 内置 CryptoPanic 抓取并写入 ChromaDB；抓取失败时仅读取 ChromaDB 缓存 |
| AI 分析 | 支持 Gemini、DeepSeek、Anthropic、OpenAI/OpenAI-compatible、Ollama 等通用模型接入 |
| 多空策略 | 分析同时输出多单和空单计划，包含入场、止损、止盈、触发条件和失效条件 |
| 推送通知 | 保留企业微信、飞书、Telegram、Discord、Slack、邮件等通知渠道 |
| Web/API | 支持本地 Web 工作台、FastAPI 接口、历史报告、回测、告警和配置管理 |

## 快速开始

```bash
pip install -r requirements.txt
copy .env.example .env
python main.py --stocks BTC
```

常用命令：

```bash
python main.py --debug
python main.py --dry-run
python main.py --stocks BTC
python main.py --schedule
python main.py --serve-only
```

## 必要配置

`.env` 中至少保留 BTC 标的，并配置一个可用模型：

```env
STOCK_LIST=BTC
GEMINI_API_KEY=
# 或 DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY / Ollama
```

BTC 新闻缓存配置可选：

```env
CRYPTOPANIC_CHROMA_PATH=
CRYPTOPANIC_CHROMA_COLLECTION=cryptopanic_news
CRYPTOPANIC_OPENCLI_PATH=
CRYPTOPANIC_REFRESH_INTERVAL_SECONDS=900
CRYPTOPANIC_MAX_AGE_HOURS=24
```

## BTC-only 约束

项目当前运行时只支持 `BTC`、`BTCUSDT`、`BTC-USD`、`BTC/USD` 等 BTC 别名，并统一规范为 `BTC`。A 股、港股、美股、股票大盘复盘、股票筛选和股票索引刷新已从默认运行逻辑中移除。

## 通知

通知配置仍沿用 `.env.example` 中的渠道变量，例如：

```env
WECHAT_WEBHOOK_URL=
FEISHU_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DISCORD_WEBHOOK_URL=
EMAIL_SENDER=
EMAIL_PASSWORD=
```

## 文档

更多配置项和部署方式见 [完整指南](docs/full-guide.md) 与 [部署说明](docs/DEPLOY.md)。

## License

[MIT License](LICENSE) © 2026 ZhuLinsen
