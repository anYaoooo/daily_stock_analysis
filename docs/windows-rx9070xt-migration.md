# Windows 新电脑迁移与 RX 9070 XT 训练环境指南

本文用于把当前 DSA/BTC Transformer 研究环境迁移到一台空白 Windows 11 电脑，并在新电脑上恢复 Web 页面、历史数据库、BTC 训练数据和研究产物。目标显卡为 AMD Radeon RX 9070 XT。

> 文档核对日期：2026-08-31。AMD 驱动、ROCm 和 PyTorch wheel 更新较快，安装显卡环境时必须再次核对 AMD 官方兼容矩阵，不要照抄旧版 wheel 地址。

## 1. 迁移结论与边界

推荐使用以下组合：

- Windows 11 64 位
- AMD 官方支持 RX 9070 XT 的 Adrenalin/ROCm 驱动
- AMD 官方 Windows PyTorch wheel
- Python 3.11（若 AMD 当前 wheel 要求其他版本，以 AMD 说明为准）
- Node.js 22 LTS
- Git for Windows
- 项目 Python 虚拟环境 `.venv`

当前训练能力有一个重要边界：

| 入口 | 当前设备行为 |
| --- | --- |
| `scripts/train_btc_transformer.py --device cuda:0` | 可使用 ROCm 暴露的 RX 9070 XT |
| `scripts/validate_btc_transformer_online.py --device cuda:0` | 可使用 ROCm 暴露的 RX 9070 XT |
| Web `/training` 页面 | 后端当前固定 `device="cpu"`，不会自动使用显卡 |

ROCm 版 PyTorch 仍使用 `torch.cuda` 和 `cuda:0` 这套 API 表示 AMD GPU，这是正常行为，不代表安装了 NVIDIA CUDA。

当前训练任务保存在后端进程内存中，且训练器不保存可恢复 checkpoint。因此：

- 正在运行的任务不能跨电脑续训；
- 后端重启后，页面不能恢复原任务状态；
- 已完成的研究产物可以通过复制 `artifacts/research/` 迁移；
- 未完成的任务需要在新电脑重新提交。

## 2. 新电脑建议配置

- 内存：建议 64GB，最低 32GB；
- 磁盘：建议 NVMe，至少预留 50GB；
- 电源和散热：满足 RX 9070 XT 厂商要求；
- Windows 电源策略：正式训练时禁止自动睡眠；
- 网络：能够访问 Python、npm、AMD 驱动和 PyTorch wheel 下载源。

AMD 官方资料：

- [Radeon Windows 兼容矩阵](https://rocm.docs.amd.com/projects/radeon/en/latest/docs/compatibility/compatibilityrad/windows/windows_compatibility.html)
- [Radeon Windows PyTorch 安装说明](https://rocm.docs.amd.com/projects/radeon/en/latest/docs/install/installrad/windows/install-pytorch.html)
- [PyTorch 本地安装入口](https://pytorch.org/get-started/locally/)

截至本文核对日期，AMD ROCm 7.2.1 官方资料已将 Radeon 9000 系列列入 Windows PyTorch 支持范围。实际安装仍以迁移当天列出的具体显卡、Windows、Python、驱动和 PyTorch 版本组合为准。

## 3. 旧电脑迁出

### 3.1 停止写入

迁移数据库前，先完成以下操作：

1. 等待需要保留的训练结束，或接受在新电脑重新训练；
2. 停止 Web 后端、计划任务和 BTC 监控进程；
3. 确认没有 Python 进程继续写入 `data/stock_analysis.db`。

不要在后端持续运行时单独复制 SQLite 主文件，否则可能漏掉 WAL 中尚未合并的数据。

### 3.2 合并 SQLite WAL

在旧电脑 PowerShell 中执行：

```powershell
Set-Location 'D:\project\AI\daily_stock_analysis'

python -c "import sqlite3; c=sqlite3.connect(r'data\stock_analysis.db'); print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()); c.close()"
```

命令成功后，`stock_analysis.db-wal` 应被截断或显著缩小。如果无法安全停止服务，则必须把下列三个文件作为同一组复制：

```text
data/stock_analysis.db
data/stock_analysis.db-wal
data/stock_analysis.db-shm
```

### 3.3 复制工作区

当前工作区可能包含尚未提交的修改，仅在新电脑执行 `git clone` 不能还原这些内容。推荐把整个仓库（包含 `.git`）复制到加密移动硬盘，同时排除可重建目录。

以下示例假设移动硬盘为 `E:`：

```powershell
$sourceRepo = 'D:\project\AI\daily_stock_analysis'
$backupRepo = 'E:\dsa-migration\daily_stock_analysis'

New-Item -ItemType Directory -Path $backupRepo -Force | Out-Null

robocopy $sourceRepo $backupRepo /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 `
  /XD .venv node_modules __pycache__ .pytest_cache logs static `
  /XF *.pyc

if ($LASTEXITCODE -gt 7) {
    throw "robocopy 备份失败，退出码：$LASTEXITCODE"
}
```

`robocopy` 的退出码 `0` 到 `7` 都不表示致命失败，只有大于 `7` 才应中止迁移。

至少确认备份中存在：

```text
.git/
.env
data/stock_analysis.db
data/btc_okx_perpetual_1h_training.csv
data/.llm_usage_hmac_secret
artifacts/
requirements.txt
requirements-ml.txt
apps/dsa-web/package-lock.json
```

`.env`、数据库和 `.llm_usage_hmac_secret` 可能包含密钥或隐私信息，迁移介质应加密，不要上传到公共网盘或提交到 Git。

### 3.4 保存校验值

```powershell
Get-FileHash -Algorithm SHA256 `
  'D:\project\AI\daily_stock_analysis\data\stock_analysis.db', `
  'D:\project\AI\daily_stock_analysis\data\btc_okx_perpetual_1h_training.csv'
```

保存输出，迁移后用于确认数据库和训练 CSV 没有损坏。

## 4. 新电脑安装基础环境

以管理员身份打开 PowerShell，安装 Git 和 Node.js：

```powershell
winget install --id Git.Git -e
winget install --id OpenJS.NodeJS.LTS -e
```

Python 版本先以 AMD 当前 Windows PyTorch wheel 的要求为准。若官方仍支持 Python 3.11，可执行：

```powershell
winget install --id Python.Python.3.11 -e
```

安装后关闭并重新打开 PowerShell：

```powershell
git --version
python --version
node --version
npm --version
```

推荐同时安装 Microsoft Visual C++ 2015–2022 Redistributable。若 Python 包构建时报编译工具缺失，再安装 Visual Studio Build Tools，不必一开始安装完整 Visual Studio。

## 5. 安装 RX 9070 XT 驱动与 PyTorch

### 5.1 驱动

1. 从 AMD 官方页面下载 RX 9070 XT 对应的 Adrenalin/ROCm 兼容驱动；
2. 不要只依赖 Windows Update 自动安装的显示驱动；
3. 安装完成后重启 Windows；
4. 在设备管理器和 AMD Software 中确认显卡识别正常。

### 5.2 恢复项目后创建虚拟环境

假设目标目录为 `D:\project\AI\daily_stock_analysis`：

```powershell
Set-Location 'D:\project\AI\daily_stock_analysis'

python -m venv .venv
& '.\.venv\Scripts\Activate.ps1'

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止激活脚本，可仅为当前用户设置签名策略：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

重新打开 PowerShell 后再次激活虚拟环境。

### 5.3 安装 AMD 官方 PyTorch wheel

在同一个 `.venv` 中，严格执行 AMD 官方安装页面针对当前组合给出的命令：

- RX 9070 XT；
- Windows 11；
- 当前稳定 ROCm；
- 当前 Python 版本；
- PyTorch训练环境。

不要直接用下面的普通命令代替 AMD wheel：

```powershell
# 不要用于安装 RX 9070 XT 加速版
python -m pip install torch
```

也不要在 AMD wheel 安装完成后执行会升级或替换 `torch` 的通用命令。仓库的 `requirements-ml.txt` 只声明 `torch>=2.2`，不能保证安装到 AMD GPU 版本。推荐顺序始终是：

1. 安装 `requirements.txt`；
2. 按 AMD 官方命令安装或替换 PyTorch；
3. 立即执行 GPU 验证；
4. 后续避免单独升级 `torch`。

### 5.4 验证 GPU

```powershell
python -c "import torch; print('torch=', torch.__version__); print('hip=', torch.version.hip); print('available=', torch.cuda.is_available()); print('device=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)"
```

期望结果：

```text
hip=非空版本号
available=True
device=AMD Radeon RX 9070 XT（或对应 AMD 设备名称）
```

再验证矩阵计算和反向传播：

```powershell
python -c "import torch; x=torch.randn(2048,2048,device='cuda',requires_grad=True); y=(x@x).mean(); y.backward(); print('GPU backward OK:',torch.cuda.get_device_name(0))"
```

如果 `torch.version.hip` 为 `None` 或 `torch.cuda.is_available()` 为 `False`，先不要运行正式训练，参见本文的故障排查部分。

## 6. 在新电脑恢复项目

以下示例假设备份位于 `E:`：

```powershell
$backupRepo = 'E:\dsa-migration\daily_stock_analysis'
$targetRepo = 'D:\project\AI\daily_stock_analysis'

New-Item -ItemType Directory -Path $targetRepo -Force | Out-Null
robocopy $backupRepo $targetRepo /E /COPY:DAT /DCOPY:DAT /R:2 /W:2

if ($LASTEXITCODE -gt 7) {
    throw "robocopy 恢复失败，退出码：$LASTEXITCODE"
}
```

恢复后检查当前代码和未提交修改是否完整：

```powershell
Set-Location 'D:\project\AI\daily_stock_analysis'
git status --short
git rev-parse HEAD
git remote -v
```

不要在尚未确认迁移完整之前执行 `git reset --hard`、`git clean` 或覆盖式 checkout。

重新计算哈希并和旧电脑记录比较：

```powershell
Get-FileHash -Algorithm SHA256 `
  '.\data\stock_analysis.db', `
  '.\data\btc_okx_perpetual_1h_training.csv'
```

## 7. 构建 Web 页面

```powershell
Set-Location 'D:\project\AI\daily_stock_analysis\apps\dsa-web'

npm ci
npm run lint
npm run build
```

构建产物写入仓库根目录的 `static/`。随后返回仓库根目录：

```powershell
Set-Location 'D:\project\AI\daily_stock_analysis'
```

## 8. 检查 `.env`

本机使用建议至少确认：

```dotenv
WEBUI_ENABLED=true
WEBUI_HOST=127.0.0.1
WEBUI_PORT=8000
DATABASE_PATH=./data/stock_analysis.db
```

还要检查：

- 是否包含旧电脑专用的绝对路径；
- LLM API Key、新闻源、代理和 Webhook 是否仍有效；
- `DATABASE_PATH` 是否指向已迁移数据库；
- `.env` 是否仍被 `.gitignore` 忽略；
- 是否存在依赖旧电脑 IP、盘符或用户名的配置。

仅在本机访问时保持 `WEBUI_HOST=127.0.0.1`。需要让局域网其他电脑访问时才使用 `0.0.0.0`，并同时：

- 启用管理员认证；
- 使用 Windows 防火墙限制允许访问的网段；
- 不要把 8000 端口直接暴露到公网；
- 不要在未认证状态下开放训练和配置接口。

## 9. 启动与页面验收

```powershell
Set-Location 'D:\project\AI\daily_stock_analysis'
& '.\.venv\Scripts\Activate.ps1'

python main.py --serve-only
```

在浏览器访问：

```text
http://127.0.0.1:8000/
http://127.0.0.1:8000/training
http://127.0.0.1:8000/api/health
```

此时 Web 页面可以使用，但 `/training` 页面提交的任务仍固定运行在 CPU。不要仅凭任务能启动就认为 RX 9070 XT 已被页面调用。

## 10. GPU 训练验收

### 10.1 轻量冒烟训练

先运行一轮缩小规模的 Fusion 训练，确认数据加载、前向、反向和产物写入都能完成：

```powershell
python scripts/train_btc_transformer.py `
  --architecture fusion `
  --device cuda:0 `
  --sequence-length 64 `
  --d-model 32 `
  --heads 4 `
  --layers 1 `
  --epochs 1 `
  --batch-size 64 `
  --folds 1 `
  --min-train-samples 1008 `
  --validation-samples 32 `
  --output artifacts\btc-fusion-gpu-smoke.json
```

冒烟产物只证明 GPU 训练链路可运行，不用于判断模型效果。

### 10.2 正式单 seed 训练

```powershell
python scripts/train_btc_transformer.py `
  --architecture fusion `
  --device cuda:0 `
  --epochs 30 `
  --batch-size 128 `
  --folds 12 `
  --min-train-samples 5000 `
  --output artifacts\btc-fusion-rx9070xt-v5.json
```

如果显存不足，依次把 batch size 调整为：

```text
128 → 64 → 32
```

不要同时降低训练窗口、折数、epoch 和模型大小后再把结果与正式实验直接比较。

### 10.3 多 seed 研究

多 seed 会显著增加训练时间，建议先完成单 seed 验收：

```powershell
python scripts/train_btc_transformer.py `
  --architecture fusion `
  --device cuda:0 `
  --research `
  --epochs 30 `
  --batch-size 64 `
  --seeds 7,13,29,43,71 `
  --output artifacts\research\btc-fusion-rx9070xt-v5.json
```

正式训练期间关闭系统自动睡眠，并保留足够磁盘空间。

## 11. 页面使用 GPU 的后续改造

当前 Web 训练后端在 `api/v1/endpoints/btc_training.py` 创建训练配置时固定传入 `device="cpu"`。迁移环境本身不会改变这一行为。

建议后续新增配置：

```dotenv
BTC_TRANSFORMER_DEVICE=auto
```

推荐语义：

- `auto`：`torch.cuda.is_available()` 为真时使用 `cuda:0`，否则使用 CPU；
- `cpu`：强制 CPU；
- `cuda:0`：强制第一块 ROCm/CUDA 设备，设备不可用时明确失败；
- API任务结果记录最终设备，避免静默回退；
- Web 页面展示实际设备和显卡名称。

在这项代码改造完成并验证前，页面训练与命令行 GPU 训练应视为两个不同入口。

## 12. 常见故障

### 12.1 `torch.version.hip` 是 `None`

可能原因：安装了 PyPI CPU 版或不匹配的 PyTorch。

处理顺序：

1. 激活正确的 `.venv`；
2. 执行 `python -m pip show torch` 检查安装位置；
3. 按 AMD 官方说明卸载冲突版本；
4. 重新安装匹配 Windows、Python 和 RX 9070 XT 的 AMD wheel；
5. 再次验证 `torch.version.hip`。

### 12.2 `torch.cuda.is_available()` 为 `False`

检查：

- AMD 驱动是否与官方 PyTorch wheel 对应；
- Windows 是否已在驱动安装后重启；
- Python架构是否为 64 位；
- 是否在正确虚拟环境中；
- RX 9070 XT 是否出现在设备管理器且无错误码；
- AMD 官方兼容矩阵是否明确支持当前组合。

### 12.3 页面能训练，但显卡占用为零

这是当前预期行为：页面后端固定 CPU。改用命令行 `--device cuda:0`，或先完成第 11 节的页面设备配置改造。

### 12.4 `out of memory`

先降低 `--batch-size`，不要优先修改标签、折数或模型结构。可按 `128 → 64 → 32` 逐级测试。

### 12.5 数据库报 `database is locked`

- 确认没有同时运行旧、新两套后端；
- 确认数据库不是直接放在同步网盘或网络共享目录；
- 迁移时把 WAL/SHM 一并处理；
- 再次停止服务并执行 `PRAGMA wal_checkpoint(TRUNCATE)`。

### 12.6 PowerShell 无法激活 `.venv`

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

关闭并重新打开 PowerShell，然后执行：

```powershell
& 'D:\project\AI\daily_stock_analysis\.venv\Scripts\Activate.ps1'
```

## 13. 最终验收清单

- [ ] 旧电脑已停止写入数据库；
- [ ] 数据库 WAL 已 checkpoint，或 DB/WAL/SHM 已成组复制；
- [ ] 整个工作区（含 `.git` 和未提交修改）已迁移；
- [ ] `.env`、数据库、BTC CSV、HMAC secret、artifacts 已迁移；
- [ ] 数据库和 BTC CSV 的 SHA256 与旧电脑一致；
- [ ] `git status --short` 与旧电脑预期一致；
- [ ] Python、Node.js、npm 和 Git 可用；
- [ ] `.venv` 已重新创建，没有复制旧电脑虚拟环境；
- [ ] `requirements.txt` 安装成功；
- [ ] AMD 官方 PyTorch wheel 安装成功；
- [ ] `torch.version.hip` 非空；
- [ ] `torch.cuda.is_available()` 为 `True`；
- [ ] RX 9070 XT 反向传播测试通过；
- [ ] `npm run lint` 和 `npm run build` 通过；
- [ ] `/api/health`、首页和 `/training` 可访问；
- [ ] CLI GPU 冒烟训练完成并生成 JSON；
- [ ] 已知晓 Web 页面当前仍固定使用 CPU；
- [ ] 正式训练前已关闭自动睡眠并确认产物目录空间充足。

## 14. 回滚方式

迁移失败时不要修改或删除旧电脑工作区。回滚步骤：

1. 停止新电脑所有 DSA 进程；
2. 保留新电脑日志和错误信息；
3. 回到旧电脑原工作区继续使用；
4. 重新生成数据库 checkpoint 和文件哈希；
5. 修正失败环节后重新复制到新的目标目录，不在损坏目录上叠加不明修改。

在新电脑通过全部验收并稳定运行一段时间前，不要清理旧电脑数据库、训练 CSV 或研究产物。
