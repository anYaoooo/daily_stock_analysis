#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill OKX BTC perpetual candles into SQLite for model training.

The default range starts at 2020-02-01 UTC, when aligned OKX trade and mark
price candles are available for BTC-USDT-SWAP. Historical funding is omitted
because OKX does not expose a reliable complete funding series for the full
range; those rows are explicitly stored with ``funding_complete=false``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.services.crypto_market_data_service import CryptoMarketDataService  # noqa: E402

DEFAULT_START = datetime(2020, 2, 1, tzinfo=timezone.utc)
DEFAULT_EXPORT_PATH = Path("data/btc_okx_perpetual_1h_training.csv")


def _parse_utc_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise argparse.ArgumentTypeError("时间不能为空")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "时间格式应为 YYYY-MM-DD 或 ISO-8601，例如 2024-01-01T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_time(value: Any) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _progress(event: dict[str, Any]) -> None:
    status = "跳过" if event["status"] == "skipped" else "写入"
    print(
        f"[{event['index']:>3}/{event['total']}] {status} "
        f"{_format_time(event['start_at'])} ~ {_format_time(event['end_at'])} "
        f"bars={event['bar_count']}",
        flush=True,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="分块回填 OKX BTC-USDT 永续合约历史 K 线，供机器学习训练和研究使用。"
    )
    parser.add_argument(
        "--start",
        type=_parse_utc_datetime,
        default=DEFAULT_START,
        help="起始 bar 开盘时间（UTC，含），默认 2020-02-01。",
    )
    parser.add_argument(
        "--end",
        type=_parse_utc_datetime,
        help="结束 bar 开盘时间（UTC，含）；默认最新已闭合 bar。",
    )
    parser.add_argument(
        "--period",
        choices=("hourly", "daily"),
        default="hourly",
        help="K 线周期，默认 hourly。",
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=30,
        help="每个远端请求分块覆盖的自然日数，默认 30。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使 SQLite 中区间完整也重新抓取并 UPSERT。",
    )
    parser.add_argument(
        "--export-csv",
        nargs="?",
        type=Path,
        const=DEFAULT_EXPORT_PATH,
        help=f"回填后导出 CSV；未指定路径时使用 {DEFAULT_EXPORT_PATH.as_posix()}。",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    service = CryptoMarketDataService()
    try:
        summary = service.backfill_perpetual_history(
            "BTC",
            start_at=args.start,
            end_at=args.end,
            period=args.period,
            venue="okx",
            margin_mode="isolated",
            chunk_days=args.chunk_days,
            force=args.force,
            progress_callback=_progress,
        )
        if args.export_csv is not None:
            exported = service.export_history_csv(
                args.export_csv,
                code="BTC",
                start_at=summary["range_start"],
                end_at=summary["range_end"],
                period=args.period,
                instrument_type="perpetual",
                venue="okx",
            )
            summary["export_csv"] = str(exported)
    except Exception as exc:
        print(f"回填失败: {exc}", file=sys.stderr)
        return 1

    printable = {
        **summary,
        "range_start": _format_time(summary.get("range_start")),
        "range_end": _format_time(summary.get("range_end")),
        "coverage_start": _format_time(summary.get("coverage_start")),
        "coverage_end": _format_time(summary.get("coverage_end")),
        "first_missing_at": _format_time(summary.get("first_missing_at")),
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    if summary["missing_bars"] or summary["execution_missing_bars"] or summary["mark_missing_bars"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
