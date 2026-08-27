# -*- coding: utf-8 -*-
"""BTC 历史回测回填重评脚本。

用途：将全部 BTC 历史分析记录用当前引擎版本 force 重评一遍，
使所有回测结果落在同一套引擎/测量代码版本上。

阶段：
  python scripts/rebacktest_btc_history.py backup   # 用 sqlite backup API 备份数据库
  python scripts/rebacktest_btc_history.py inspect  # 摸底：记录数 / 结果版本分布 / 汇总口径
  python scripts/rebacktest_btc_history.py run      # 全量 force 重评（自动重算汇总）
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "stock_analysis.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"

BTC_CODES = ("BTC", "BTC-USDT", "BTCUSDT", "BTCUSD", "BTC-USD", "BTC/USD", "BTC_USDT")


def do_backup() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"stock_analysis_pre_rebacktest_{stamp}.db"
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"backup ok: {target} ({size_mb:.1f} MB)")


def do_inspect() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in BTC_CODES)

    total = cur.execute(
        f"SELECT COUNT(*) AS n, MIN(created_at) AS earliest, MAX(created_at) AS latest "
        f"FROM analysis_history WHERE UPPER(code) IN ({placeholders}) "
        f"AND (report_type IS NULL OR report_type != 'market_review')",
        BTC_CODES,
    ).fetchone()
    print(f"BTC analysis_history: {total['n']} 条, {total['earliest']} ~ {total['latest']}")

    print("\ncrypto_backtest_results 按引擎版本分布:")
    for row in cur.execute(
        "SELECT engine_version, COUNT(*) AS n FROM crypto_backtest_results GROUP BY engine_version ORDER BY n DESC"
    ):
        print(f"  {row['engine_version']}: {row['n']}")

    print("\ncrypto_backtest_results 按 eval_status / outcome:")
    for row in cur.execute(
        "SELECT engine_version, eval_status, outcome, COUNT(*) AS n FROM crypto_backtest_results "
        "GROUP BY engine_version, eval_status, outcome ORDER BY engine_version, n DESC"
    ):
        print(f"  {row['engine_version']} | {row['eval_status']} | {row['outcome']}: {row['n']}")

    missing = cur.execute(
        f"SELECT id FROM analysis_history WHERE UPPER(code) IN ({placeholders}) "
        f"AND (report_type IS NULL OR report_type != 'market_review') "
        f"AND id NOT IN (SELECT DISTINCT analysis_history_id FROM crypto_backtest_results) ORDER BY id",
        BTC_CODES,
    ).fetchall()
    print(f"\n无任何回测结果的历史记录 ({len(missing)} 条): {[r['id'] for r in missing]}")

    print("\ncrypto_backtest_summaries (scope=overall):")
    cols = [r["name"] for r in cur.execute("PRAGMA table_info(crypto_backtest_summaries)")]
    wanted = [
        c for c in cols
        if c in {
            "engine_version", "scope", "code", "win_count", "loss_count", "neutral_count",
            "direction_accuracy_pct", "direction_accuracy_raw_pct", "updated_at",
        } or "count" in c or "pct" in c
    ]
    for row in cur.execute(
        f"SELECT {', '.join(wanted)} FROM crypto_backtest_summaries WHERE scope='overall'"
    ):
        print("  " + json.dumps(dict(row), ensure_ascii=False, default=str))
    conn.close()


def do_run() -> None:
    from src.services.crypto_backtest_service import CryptoBacktestService

    service = CryptoBacktestService()
    engine_version = service._engine_version()
    rows, total = service.repo.get_history_records(offset=0, limit=100000)
    ids = [int(row.id) for row in rows if row.id is not None]
    print(f"engine_version={engine_version}, 待重评 BTC 历史记录 {len(ids)} 条 (repo total={total})")
    if not ids:
        print("没有可重评的记录，退出。")
        return

    started = time.time()
    stats = service.run_selected_backtests(analysis_history_ids=ids, force=True)
    elapsed = time.time() - started
    print(f"\n重评完成，耗时 {elapsed:.1f}s")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def do_missing() -> None:
    """检查始终没有回测结果的历史记录：raw_result 是否存在可抽取计划。"""
    from src.services.crypto_backtest_service import CryptoBacktestService

    service = CryptoBacktestService()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in BTC_CODES)
    rows = conn.execute(
        f"SELECT id, created_at, LENGTH(raw_result) AS raw_len, raw_result FROM analysis_history "
        f"WHERE UPPER(code) IN ({placeholders}) "
        f"AND (report_type IS NULL OR report_type != 'market_review') "
        f"AND id NOT IN (SELECT DISTINCT analysis_history_id FROM crypto_backtest_results) ORDER BY id",
        BTC_CODES,
    ).fetchall()
    conn.close()
    for row in rows:
        raw = row["raw_result"] or ""
        try:
            payload = json.loads(raw) if raw else None
            parse_ok = payload is not None
        except Exception:
            payload = None
            parse_ok = False
        keys = []
        top_keys = list(payload.keys())[:12] if isinstance(payload, dict) else []
        if isinstance(payload, dict):
            battle = payload.get("battle_plan") if isinstance(payload.get("battle_plan"), dict) else payload
            keys = [k for k in ("long_plan", "short_plan", "intraday_plan") if isinstance(battle.get(k), dict)]
        print(
            f"id={row['id']} created={row['created_at']} raw_len={row['raw_len']} "
            f"json_ok={parse_ok} plan_keys={keys} top_keys={top_keys}"
        )


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase == "backup":
        do_backup()
    elif phase == "inspect":
        do_inspect()
    elif phase == "run":
        do_run()
    elif phase == "missing":
        do_missing()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
