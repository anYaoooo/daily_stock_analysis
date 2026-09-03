param(
    [ValidateSet("all", "syntax", "flake8", "deterministic", "offline-tests")]
    [string]$Phase = "all"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

function Invoke-Python {
    param([string[]]$Arguments)
    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Syntax {
    Write-Host "==> backend-gate: Python syntax check"
    $files = @(
        "main.py", "src/config.py", "src/auth.py", "src/analyzer.py",
        "src/notification.py", "src/storage.py", "src/scheduler.py",
        "src/search_service.py", "src/market_analyzer.py", "src/stock_analyzer.py"
    )
    $files += (Get-ChildItem -LiteralPath (Join-Path $repoRoot "data_provider") -Filter "*.py" -File).FullName
    Invoke-Python (@("-m", "py_compile") + $files)
}

function Invoke-Flake8 {
    Write-Host "==> backend-gate: flake8 critical checks"
    & flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
    if ($LASTEXITCODE -ne 0) { throw "flake8 failed with exit code $LASTEXITCODE" }
}

function Invoke-Deterministic {
    Write-Host "==> backend-gate: deterministic checks"
    $codeCheck = @'
from data_provider.akshare_fetcher import _is_hk_code, _is_us_code
cases = [("AAPL", False, True), ("TSLA", False, True), ("BRK.B", False, True), ("hk00700", True, False), ("HK09988", True, False), ("600519", False, False), ("000001", False, False)]
assert all((_is_hk_code(code), _is_us_code(code)) == (hk, us) for code, hk, us in cases)
'@
    $codeCheck | & python -
    if ($LASTEXITCODE -ne 0) { throw "code recognition check failed" }

    $yfCheck = @'
from data_provider.yfinance_fetcher import YfinanceFetcher
fetcher = YfinanceFetcher()
cases = [("AAPL", "AAPL"), ("tsla", "TSLA"), ("BRK.B", "BRK.B"), ("hk00700", "0700.HK"), ("HK09988", "9988.HK"), ("600519", "600519.SS"), ("000001", "000001.SZ"), ("300750", "300750.SZ")]
assert all(fetcher._convert_stock_code(code) == expected for code, expected in cases)
'@
    $yfCheck | & python -
    if ($LASTEXITCODE -ne 0) { throw "yfinance conversion check failed" }
}

function Invoke-OfflineTests {
    Write-Host "==> backend-gate: offline test suite"
    Invoke-Python @("-m", "pytest", "-m", "not network")
}

switch ($Phase) {
    "syntax" { Invoke-Syntax }
    "flake8" { Invoke-Flake8 }
    "deterministic" { Invoke-Deterministic }
    "offline-tests" { Invoke-OfflineTests }
    "all" {
        Invoke-Syntax
        Invoke-Flake8
        Invoke-Deterministic
        Invoke-OfflineTests
        Write-Host "==> backend-gate: all checks passed"
    }
}
