"""サーバー内蔵スケジューラのテスト — 時刻判定・重複スキップ・設定保存を検証。"""

from datetime import datetime

from src.web.server import (
    _next_scheduled_run,
    _parse_scheduler_time,
    _should_run_scheduled,
)


class TestParseSchedulerTime:
    def test_valid(self):
        assert _parse_scheduler_time("07:00") == (7, 0)
        assert _parse_scheduler_time("23:45") == (23, 45)
        assert _parse_scheduler_time("0:05") == (0, 5)

    def test_invalid_falls_back_to_default(self):
        assert _parse_scheduler_time("25:00") == (7, 0)
        assert _parse_scheduler_time("abc") == (7, 0)
        assert _parse_scheduler_time("") == (7, 0)
        assert _parse_scheduler_time(None) == (7, 0)


class TestShouldRunScheduled:
    def test_before_scheduled_time(self):
        now = datetime(2026, 6, 10, 6, 59)
        assert _should_run_scheduled(now, "07:00", None) is False

    def test_first_run(self):
        now = datetime(2026, 6, 10, 7, 0)
        assert _should_run_scheduled(now, "07:00", None) is True

    def test_already_ran_today(self):
        now = datetime(2026, 6, 10, 8, 0)
        last = datetime(2026, 6, 10, 7, 1)
        assert _should_run_scheduled(now, "07:00", last) is False

    def test_next_day(self):
        now = datetime(2026, 6, 11, 7, 0)
        last = datetime(2026, 6, 10, 7, 1)
        assert _should_run_scheduled(now, "07:00", last) is True

    def test_catch_up_after_downtime(self):
        # サーバーが 7:00 に停止していた → 起動後の判定で追いつき実行
        now = datetime(2026, 6, 10, 12, 34)
        last = datetime(2026, 6, 9, 7, 0)
        assert _should_run_scheduled(now, "07:00", last) is True

    def test_no_retry_after_failure(self):
        # 失敗しても試行時刻が記録される → 翌日まで再試行しない
        now = datetime(2026, 6, 10, 7, 5)
        last = datetime(2026, 6, 10, 7, 0)
        assert _should_run_scheduled(now, "07:00", last) is False

    def test_invalid_time_uses_default(self):
        now = datetime(2026, 6, 10, 7, 30)
        assert _should_run_scheduled(now, "invalid", None) is True


class TestNextScheduledRun:
    def test_today_if_future(self):
        now = datetime(2026, 6, 10, 6, 0)
        assert _next_scheduled_run(now, "07:00") == datetime(2026, 6, 10, 7, 0)

    def test_tomorrow_if_passed(self):
        now = datetime(2026, 6, 10, 7, 0)
        assert _next_scheduled_run(now, "07:00") == datetime(2026, 6, 11, 7, 0)

    def test_tomorrow_if_just_after(self):
        now = datetime(2026, 6, 10, 8, 30)
        assert _next_scheduled_run(now, "07:00") == datetime(2026, 6, 11, 7, 0)
