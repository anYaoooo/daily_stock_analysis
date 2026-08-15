from datetime import datetime, timezone

from scripts.backfill_btc_history import DEFAULT_EXPORT_PATH, build_arg_parser


def test_backfill_cli_defaults_to_okx_hourly_training_range() -> None:
    args = build_arg_parser().parse_args([])

    assert args.start == datetime(2020, 2, 1, tzinfo=timezone.utc)
    assert args.end is None
    assert args.period == "hourly"
    assert args.chunk_days == 30
    assert args.export_csv is None


def test_backfill_cli_export_flag_uses_default_training_path() -> None:
    args = build_arg_parser().parse_args(["--export-csv"])

    assert args.export_csv == DEFAULT_EXPORT_PATH
