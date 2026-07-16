# Legacy 股票模块隔离边界

项目已切换为 BTC-only。A/H/美股数据源、股票市场复盘、基本面适配、股票索引、股票智能导入和旧 DecisionSignals Web UI 不再属于默认产品与 CI 契约。

## 默认边界

- 后端隔离清单由 `tests/conftest.py` 的 `LEGACY_STOCK_TEST_FILES` 和 `LEGACY_STOCK_TEST_NAMES_BY_FILE` 维护。前者隔离纯 legacy 文件，后者只隔离混合文件中的旧用例，避免丢失同文件的现役 BTC/API 覆盖。默认 pytest 会将命中项标记为 `legacy_stock` 并取消收集执行。
- Web 隔离清单由 `apps/dsa-web/vitest.config.ts` 的 `legacyStockTestFiles` 维护。默认 `npm test` 不执行这些文件。
- Web 不再挂载 `/decision-signals`，侧边栏不再展示 AI 建议入口，设置页不再展示股票智能导入，BTC 报告摘要不再加载旧市场复盘专用视图或股票决策信号卡。
- 隔离模块源码暂时保留，仅用于后续依赖拆除和历史数据迁移；不得从新的 BTC 代码重新引用。

## 显式审计

PowerShell 下可临时把后端隔离套件加入测试：

```powershell
$env:DSA_INCLUDE_LEGACY_STOCK_TESTS = '1'
python -m pytest -m "legacy_stock and not network"
Remove-Item Env:DSA_INCLUDE_LEGACY_STOCK_TESTS
```

Web 隔离套件使用独立配置：

```powershell
Set-Location apps/dsa-web
npm run test:legacy-stock
```

隔离套件允许保留已知失败，用于删除进度审计，不作为 BTC-only 默认 CI 的绿灯证据。新增或修改 BTC 能力时，不得通过扩大隔离清单规避回归失败。

## 后续删除顺序

1. 先移除 API、CLI、Bot 和调度入口，确认 BTC 主流程不再导入对应模块。
2. 再删除 Web/API schema、服务、数据源和存储兼容代码。
3. 最后删除隔离测试与依赖，并从清单移除对应文件名。

每次删除必须运行默认后端离线测试、Web 测试、lint 和 build；涉及用户可见入口时同步更新中英文指南和 `docs/CHANGELOG.md`。
