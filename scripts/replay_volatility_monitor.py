#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replay the BTC volatility monitor over historical 1m klines.

Feeds historical prices through the real ``BTCVolatilityMonitor`` state machine
(same code path as production) and reports three tuning metrics:

- detection rate: share of real >=X% moves caught by the monitor;
- average detection delay: seconds from move start to first event/trigger;
- false positive rate: detections whose entry confirmation price was not
  reached within N minutes.

Data sources (pick one):
  --csv PATH            offline 1m klines (timestamp,open,high,low,close[,volume])
  --fetch               pull 1m klines via ccxt (--exchange/--symbol/--days)

Examples:
  python scripts/replay_volatility_monitor.py --csv data/btc_1m.csv
  python scripts/replay_volatility_monitor.py --csv data/btc_1m.csv --compare
  python scripts/replay_volatility_monitor.py --csv data/btc_1m.csv --window-tiers "1:0.4,3:0.7,5:1.0,15:1.5"
  python scripts/replay_volatility_monitor.py --fetch --days 3 --save-csv data/btc_1m.csv

Exit codes: 0 = ok, 1 = runtime error, 2 = bad arguments/input data.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.services.btc_volatility_monitor import BTCVolatilityMonitor  # noqa: E402

DEFAULT_TIERS = "1:0.4,3:0.7,5:1.0,15:1.5"


@dataclass(frozen=True)
class Kline:
    ts: float  # open time, epoch seconds
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class TrueEvent:
    direction: str  # "up" | "down"
    start_ts: float
    extreme_ts: float
    magnitude_pct: float


@dataclass
class ReplayResult:
    name: str
    detections: List[Dict[str, Any]] = field(default_factory=list)  # event_detected (incl. sweeps)
    triggers: List[Dict[str, Any]] = field(default_factory=list)  # triggered entry_signal
    sweeps: List[Dict[str, Any]] = field(default_factory=list)  # liquidity_sweep alerts
    suppressed: int = 0


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------

_TIMESTAMP_COLUMNS = ("timestamp", "time", "open_time", "opentime", "date", "datetime")


def _parse_timestamp(raw: Any) -> float:
    text = str(raw).strip()
    if not text:
        raise ValueError("empty timestamp")
    try:
        value = float(text)
    except ValueError:
        from datetime import datetime, timezone

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                # naive kline timestamps are conventionally UTC
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        raise ValueError(f"unrecognized timestamp: {text!r}")
    if value > 1e12:  # epoch milliseconds
        return value / 1000.0
    return value


def _load_klines_csv(path: Path) -> List[Kline]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header row: {path}")
        lower_to_actual = {name.strip().lower(): name for name in reader.fieldnames}
        ts_column = next((lower_to_actual[c] for c in _TIMESTAMP_COLUMNS if c in lower_to_actual), None)
        required = ["open", "high", "low", "close"]
        missing = [c for c in required if c not in lower_to_actual]
        if ts_column is None or missing:
            raise ValueError(
                f"CSV must contain timestamp/open/high/low/close columns; missing: "
                f"{(['timestamp'] if ts_column is None else []) + missing}"
            )
        klines: List[Kline] = []
        for row in reader:
            try:
                klines.append(
                    Kline(
                        ts=_parse_timestamp(row[ts_column]),
                        open=float(row[lower_to_actual["open"]]),
                        high=float(row[lower_to_actual["high"]]),
                        low=float(row[lower_to_actual["low"]]),
                        close=float(row[lower_to_actual["close"]]),
                    )
                )
            except (TypeError, ValueError):
                continue
    klines.sort(key=lambda item: item.ts)
    return klines


def _fetch_klines_ccxt(exchange_id: str, symbol: str, days: int) -> List[Kline]:
    try:
        import ccxt  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("ccxt is required for --fetch; pip install ccxt") from exc

    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"unknown ccxt exchange: {exchange_id}")
    exchange = exchange_class({"enableRateLimit": True})
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days) * 24 * 60 * 60 * 1000
    klines: List[Kline] = []
    since = start_ms
    while since < end_ms:
        rows = exchange.fetch_ohlcv(symbol, timeframe="1m", since=since, limit=300)
        if not rows:
            break
        for row in rows:
            ts = float(row[0]) / 1000.0
            if start_ms <= row[0] <= end_ms:
                klines.append(Kline(ts=ts, open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4])))
        last_ts = int(rows[-1][0])
        since = last_ts + 60_000
        if len(rows) < 2:
            break
        print(f"[replay] fetched {len(klines)} klines so far...", flush=True)
    klines.sort(key=lambda item: item.ts)
    return klines


def _save_klines_csv(path: Path, klines: List[Kline]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close"])
        for item in klines:
            writer.writerow([int(item.ts), item.open, item.high, item.low, item.close])


# ---------------------------------------------------------------------------
# sampling: turn 1m klines into the quote stream the monitor would have seen
# ---------------------------------------------------------------------------

def _intrabar_path(kline: Kline) -> Tuple[float, float, float, float]:
    """Approximate intra-minute price path (bullish: open->low->high->close)."""
    if kline.close >= kline.open:
        return (kline.open, kline.low, kline.high, kline.close)
    return (kline.open, kline.high, kline.low, kline.close)


def _sample_points(klines: List[Kline], interval_seconds: int) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    if interval_seconds >= 60:
        step = max(1, interval_seconds // 60)
        for index in range(0, len(klines), step):
            item = klines[index]
            points.append((item.ts + 60.0, item.close))
        return points
    # sub-minute polling: walk the approximated intra-bar path
    for item in klines:
        path = _intrabar_path(item)
        segments = len(path) - 1
        ts = item.ts
        while ts < item.ts + 60.0:
            progress = min(1.0, (ts - item.ts) / 60.0)
            position = progress * segments
            seg = min(int(position), segments - 1)
            frac = position - seg
            price = path[seg] + (path[seg + 1] - path[seg]) * frac
            points.append((ts, price))
            ts += float(interval_seconds)
    return points


# ---------------------------------------------------------------------------
# ground truth: real volatility events in the kline series
# ---------------------------------------------------------------------------

def _scan_true_events(
    klines: List[Kline],
    *,
    window_minutes: int,
    min_pct: float,
    merge_gap_bars: int = 2,
) -> List[TrueEvent]:
    """Label real volatility events with a zigzag-style windowed scan.

    For each bar j, measure the move from the lowest low / highest high of the
    trailing ``window_minutes`` bars to the current bar's high / low. While the
    move stays >= ``min_pct`` the event is active: its start is the anchor bar
    and its extreme tracks the furthest price. Runs of active bars on the same
    side merge into one event (short pauses up to ``merge_gap_bars`` tolerated),
    so a single move is counted once with its true extent.
    """
    events: List[TrueEvent] = []
    active: Optional[Dict[str, Any]] = None
    gap = 0

    def close_active() -> None:
        nonlocal active
        if active is None:
            return
        anchor = klines[active["start_index"]]
        anchor_price = anchor.low if active["direction"] == "up" else anchor.high
        if anchor_price > 0:
            magnitude = abs(active["extreme_price"] - anchor_price) / anchor_price * 100.0
            events.append(
                TrueEvent(
                    direction=active["direction"],
                    start_ts=anchor.ts,
                    extreme_ts=klines[active["extreme_index"]].ts,
                    magnitude_pct=magnitude,
                )
            )
        active = None

    for j in range(len(klines)):
        lo = max(0, j - window_minutes)
        up_anchor = min(range(lo, j + 1), key=lambda k: klines[k].low)
        down_anchor = max(range(lo, j + 1), key=lambda k: klines[k].high)
        up_ret = 0.0
        if klines[up_anchor].low > 0:
            up_ret = (klines[j].high - klines[up_anchor].low) / klines[up_anchor].low * 100.0
        down_ret = 0.0
        if klines[down_anchor].high > 0:
            down_ret = (klines[down_anchor].high - klines[j].low) / klines[down_anchor].high * 100.0

        direction: Optional[str] = None
        if up_ret >= min_pct and up_ret >= down_ret:
            direction = "up"
        elif down_ret >= min_pct:
            direction = "down"

        if direction is None:
            if active is not None:
                gap += 1
                if gap > merge_gap_bars:
                    close_active()
                    gap = 0
            continue

        anchor_index = up_anchor if direction == "up" else down_anchor
        extreme_price = klines[j].high if direction == "up" else klines[j].low
        if active is None or active["direction"] != direction:
            close_active()
            active = {
                "direction": direction,
                "start_index": anchor_index,
                "extreme_index": j,
                "extreme_price": extreme_price,
            }
        else:
            if (direction == "up" and extreme_price > active["extreme_price"]) or (
                direction == "down" and extreme_price < active["extreme_price"]
            ):
                active["extreme_price"] = extreme_price
                active["extreme_index"] = j
            if klines[anchor_index].ts < klines[active["start_index"]].ts:
                active["start_index"] = anchor_index
        gap = 0

    close_active()
    return events


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def _build_config(args: argparse.Namespace, window_tiers: str) -> SimpleNamespace:
    return SimpleNamespace(
        btc_volatility_monitor_enabled=True,
        btc_volatility_monitor_symbol="BTC",
        btc_volatility_monitor_window_minutes=args.window_minutes,
        btc_volatility_monitor_window_tiers=window_tiers,
        btc_volatility_monitor_spike_revert_pct=args.spike_revert_pct,
        btc_volatility_monitor_early_warning_pct=args.early_warning_pct,
        btc_volatility_monitor_threshold_pct=args.threshold_pct,
        btc_volatility_monitor_cooldown_minutes=args.cooldown_minutes,
        btc_volatility_monitor_cooldown_allow_reversal=args.cooldown_allow_reversal,
        btc_volatility_monitor_confirmation_samples=args.confirmation_samples,
        btc_volatility_monitor_entry_confirmation_pct=args.entry_confirmation_pct,
        btc_volatility_monitor_invalidation_pct=args.invalidation_pct,
        btc_volatility_monitor_max_watch_minutes=args.max_watch_minutes,
        btc_volatility_monitor_adaptive_threshold_enabled=args.adaptive_threshold,
        btc_volatility_monitor_adaptive_k=args.adaptive_k,
        btc_volatility_monitor_adaptive_min_pct=args.adaptive_min_pct,
        btc_volatility_monitor_adaptive_max_pct=args.adaptive_max_pct,
        btc_volatility_monitor_adaptive_lookback_minutes=args.adaptive_lookback_minutes,
        btc_volatility_monitor_velocity_enabled=args.velocity,
        btc_volatility_monitor_velocity_mult=args.velocity_mult,
        btc_volatility_monitor_fast_confirmation_enabled=args.fast_confirmation,
        btc_volatility_monitor_fast_confirmation_mult=args.fast_confirmation_mult,
    )


def _run_replay(
    name: str,
    points: List[Tuple[float, float]],
    config: Any,
) -> ReplayResult:
    state = {"index": -1}

    def quote_fetcher(_symbol: str) -> Dict[str, Any]:
        return {"price": points[state["index"]][1], "provider_timestamp": "replay"}

    def now_provider() -> float:
        return points[state["index"]][0]

    monitor = BTCVolatilityMonitor(quote_fetcher=quote_fetcher, now_provider=now_provider)
    result = ReplayResult(name=name)
    for index in range(len(points)):
        state["index"] = index
        stats = monitor.run_once(config)
        ts = points[index][0]
        if stats.get("event_detected"):
            entry = {"ts": ts, "reason": stats.get("reason", ""), **stats}
            result.detections.append(entry)
            if stats.get("trigger_reason") == "liquidity_sweep":
                result.sweeps.append(entry)
        if stats.get("triggered"):
            result.triggers.append({"ts": ts, **stats})
        result.suppressed += int(stats.get("suppressed", 0))
    return result


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def _price_reached(
    klines: List[Kline],
    *,
    after_ts: float,
    minutes: int,
    direction: str,
    target_price: float,
) -> Optional[bool]:
    """Whether klines after ``after_ts`` reach ``target_price`` within ``minutes``.

    Returns None when the check window extends beyond the available data.
    """
    deadline = after_ts + minutes * 60
    usable = False
    for item in klines:
        if item.ts < after_ts:
            continue
        if item.ts > deadline:
            usable = True
            break
        if direction == "up" and item.high >= target_price:
            return True
        if direction == "down" and item.low <= target_price:
            return True
    if not usable and klines and klines[-1].ts < deadline:
        return None
    return False


def _compute_metrics(
    events: List[TrueEvent],
    result: ReplayResult,
    klines: List[Kline],
    *,
    false_positive_minutes: int,
) -> Dict[str, Any]:
    actionable = [d for d in result.detections if d.get("trigger_reason") != "liquidity_sweep"]
    matched_event_ids = set()
    delays: List[float] = []
    for event_id, event in enumerate(events):
        hits = [
            d for d in actionable
            if d.get("direction") == event.direction and event.start_ts <= d["ts"] <= event.extreme_ts + 60
        ]
        if not hits:
            hits = [
                t for t in result.triggers
                if t.get("direction") == event.direction and event.start_ts <= t["ts"] <= event.extreme_ts + 60
            ]
        if hits:
            matched_event_ids.add(event_id)
            delays.append(min(h["ts"] for h in hits) - event.start_ts)
    detected = len(matched_event_ids)
    detection_rate = (detected / len(events)) if events else None
    avg_delay = (sum(delays) / len(delays)) if delays else None

    false_positives = 0
    fp_judged = 0
    fp_dropped = 0
    for detection in actionable:
        entry_price = detection.get("entry_price")
        direction = detection.get("direction")
        if not isinstance(entry_price, (int, float)) or direction not in {"up", "down"}:
            continue
        reached = _price_reached(
            klines,
            after_ts=detection["ts"],
            minutes=false_positive_minutes,
            direction=direction,
            target_price=float(entry_price),
        )
        if reached is None:
            fp_dropped += 1
            continue
        fp_judged += 1
        if not reached:
            false_positives += 1
    false_positive_rate = (false_positives / fp_judged) if fp_judged else None

    invalidated = 0
    inv_judged = 0
    for trigger in result.triggers:
        invalidation_price = trigger.get("invalidation_price")
        direction = trigger.get("direction")
        if not isinstance(invalidation_price, (int, float)) or direction not in {"up", "down"}:
            continue
        opposite = "down" if direction == "up" else "up"
        hit_stop = _price_reached(
            klines,
            after_ts=trigger["ts"],
            minutes=false_positive_minutes,
            direction=opposite,
            target_price=float(invalidation_price),
        )
        if hit_stop is None:
            continue
        inv_judged += 1
        if hit_stop:
            invalidated += 1
    trigger_invalidation_rate = (invalidated / inv_judged) if inv_judged else None

    return {
        "name": result.name,
        "events_total": len(events),
        "events_detected": detected,
        "detection_rate": detection_rate,
        "avg_detection_delay_seconds": avg_delay,
        "detections": len(actionable),
        "sweeps": len(result.sweeps),
        "triggers": len(result.triggers),
        "suppressed": result.suppressed,
        "false_positives": false_positives,
        "fp_judged": fp_judged,
        "fp_dropped": fp_dropped,
        "false_positive_rate": false_positive_rate,
        "trigger_invalidation_rate": trigger_invalidation_rate,
    }


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def _pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _seconds(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.0f}s"


def _print_metrics(metrics: Dict[str, Any]) -> None:
    print(f"\n===== 配置: {metrics['name']} =====")
    print(f"  真实波动事件: {metrics['events_detected']}/{metrics['events_total']} 被检出 "
          f"(检出率 {_pct(metrics['detection_rate'])})")
    print(f"  平均检出延迟: {_seconds(metrics['avg_detection_delay_seconds'])}")
    print(f"  检出事件(event_detected): {metrics['detections']}  "
          f"插针告警: {metrics['sweeps']}  入场触发: {metrics['triggers']}  冷却抑制: {metrics['suppressed']}")
    fp_note = f" ({metrics['fp_dropped']} 个因数据不足未判定)" if metrics["fp_dropped"] else ""
    print(f"  误报: {metrics['false_positives']}/{metrics['fp_judged']} 检出后未达入场确认价 "
          f"(误报率 {_pct(metrics['false_positive_rate'])}){fp_note}")
    print(f"  触发后触及失效价比例: {_pct(metrics['trigger_invalidation_rate'])}")


def _print_compare(all_metrics: List[Dict[str, Any]]) -> None:
    print("\n===== 对比汇总 =====")
    header = f"{'配置':<16}{'检出率':>10}{'平均延迟':>10}{'误报率':>10}{'触发失效':>10}{'触发数':>8}"
    print(header)
    print("-" * len(header))
    for metrics in all_metrics:
        print(
            f"{metrics['name']:<16}"
            f"{_pct(metrics['detection_rate']):>10}"
            f"{_seconds(metrics['avg_detection_delay_seconds']):>10}"
            f"{_pct(metrics['false_positive_rate']):>10}"
            f"{_pct(metrics['trigger_invalidation_rate']):>10}"
            f"{metrics['triggers']:>8}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="回放 BTC 波动监控状态机,输出检出率/检出延迟/误报率")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="1m K 线 CSV(timestamp,open,high,low,close)")
    source.add_argument("--fetch", action="store_true", help="通过 ccxt 拉取 1m K 线")
    parser.add_argument("--exchange", default="okx", help="ccxt 交易所 id(--fetch,默认 okx)")
    parser.add_argument("--symbol", default="BTC/USDT", help="交易对(--fetch,默认 BTC/USDT)")
    parser.add_argument("--days", type=int, default=3, help="拉取天数(--fetch,默认 3)")
    parser.add_argument("--save-csv", type=Path, help="把 --fetch 拉到的 K 线存成 CSV")

    parser.add_argument("--interval-seconds", type=int, default=60, help="模拟轮询间隔秒数(默认 60;<60 时按 K 线路径插值)")
    parser.add_argument("--window-tiers", default=DEFAULT_TIERS, help=f"多级窗口,留空走 legacy(默认 {DEFAULT_TIERS!r})")
    parser.add_argument("--compare", action="store_true", help="同时跑 legacy 单窗口与 --window-tiers 组并输出对比")
    parser.add_argument("--window-minutes", type=int, default=5, help="legacy 单窗口分钟数(默认 5)")
    parser.add_argument("--threshold-pct", type=float, default=1.0, help="legacy 触发阈值 %%(默认 1.0)")
    parser.add_argument("--early-warning-pct", type=float, default=0.3, help="预警阈值 %%(默认 0.3)")
    parser.add_argument("--spike-revert-pct", type=float, default=0.4, help="插针回落阈值 %%(默认 0.4)")
    parser.add_argument("--confirmation-samples", type=int, default=2, help="确认采样数(默认 2)")
    parser.add_argument("--entry-confirmation-pct", type=float, default=0.2, help="入场确认幅度 %%(默认 0.2)")
    parser.add_argument("--invalidation-pct", type=float, default=0.5, help="失效幅度 %%(默认 0.5)")
    parser.add_argument("--max-watch-minutes", type=int, default=20, help="机会观察上限分钟(默认 20)")
    parser.add_argument("--cooldown-minutes", type=int, default=30, help="冷却分钟(默认 30)")
    parser.add_argument("--cooldown-allow-reversal", action="store_true", help="允许反向信号破冷却")
    parser.add_argument("--adaptive-threshold", action="store_true", help="启用自适应阈值(σ 滚动估计)")
    parser.add_argument("--adaptive-k", type=float, default=2.5, help="自适应阈值 K 倍数(默认 2.5)")
    parser.add_argument("--adaptive-min-pct", type=float, default=0.4, help="自适应阈值下限 %%(默认 0.4)")
    parser.add_argument("--adaptive-max-pct", type=float, default=2.0, help="自适应阈值上限 %%(默认 2.0)")
    parser.add_argument("--adaptive-lookback-minutes", type=int, default=240, help="σ/速度基线回望分钟(默认 240)")
    parser.add_argument("--velocity", action="store_true", help="启用速度触发")
    parser.add_argument("--velocity-mult", type=float, default=3.0, help="速度触发倍率(默认 3.0)")
    parser.add_argument("--velocity-min-pct", type=float, default=0.1, help="速度触发绝对幅度下限 %%(默认 0.1)")
    parser.add_argument("--fast-confirmation", action="store_true", help="启用分级确认(暴力行情 1 采样确认)")
    parser.add_argument("--fast-confirmation-mult", type=float, default=1.5, help="暴力行情判定倍率(默认 1.5)")

    parser.add_argument("--event-min-pct", type=float, default=1.0, help="真实事件最小幅度 %%(默认 1.0)")
    parser.add_argument("--event-window-minutes", type=int, default=15, help="真实事件判定窗口分钟(默认 15)")
    parser.add_argument("--false-positive-minutes", type=int, default=30, help="误报判定窗口分钟(默认 30)")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON 指标")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.interval_seconds < 1:
        print("[replay] ERROR: --interval-seconds 必须 >= 1", file=sys.stderr)
        return 2

    try:
        if args.fetch:
            klines = _fetch_klines_ccxt(args.exchange, args.symbol, args.days)
        else:
            if not args.csv.is_file():
                print(f"[replay] ERROR: CSV 不存在: {args.csv}", file=sys.stderr)
                return 2
            klines = _load_klines_csv(args.csv)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[replay] ERROR: 加载 K 线失败: {exc}", file=sys.stderr)
        return 1

    if len(klines) < 3:
        print(f"[replay] ERROR: K 线数量不足({len(klines)} 根),无法回放", file=sys.stderr)
        return 2

    if args.save_csv and args.fetch:
        try:
            _save_klines_csv(args.save_csv, klines)
            print(f"[replay] saved {len(klines)} klines -> {args.save_csv}")
        except OSError as exc:
            print(f"[replay] ERROR: 保存 CSV 失败: {exc}", file=sys.stderr)
            return 1

    span_hours = (klines[-1].ts - klines[0].ts) / 3600.0
    print(f"[replay] klines={len(klines)} span={span_hours:.1f}h interval={args.interval_seconds}s")

    events = _scan_true_events(
        klines,
        window_minutes=max(1, args.event_window_minutes),
        min_pct=max(0.1, args.event_min_pct),
    )
    print(f"[replay] 真实波动事件(>= {args.event_min_pct}% / {args.event_window_minutes}min): {len(events)} 个")

    points = _sample_points(klines, args.interval_seconds)
    runs: List[Tuple[str, str]] = []
    if args.compare:
        runs.append(("legacy", ""))
        runs.append((f"tiers[{args.window_tiers or 'EMPTY'}]", args.window_tiers))
    else:
        label = f"tiers[{args.window_tiers}]" if args.window_tiers else "legacy"
        runs.append((label, args.window_tiers))

    all_metrics: List[Dict[str, Any]] = []
    for name, tiers_value in runs:
        config = _build_config(args, tiers_value)
        result = _run_replay(name, points, config)
        metrics = _compute_metrics(
            events,
            result,
            klines,
            false_positive_minutes=max(1, args.false_positive_minutes),
        )
        _print_metrics(metrics)
        all_metrics.append(metrics)

    if args.compare and len(all_metrics) > 1:
        _print_compare(all_metrics)

    if args.json:
        import json

        print("\n" + json.dumps(all_metrics, ensure_ascii=False, indent=2))

    print("\n[replay] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
