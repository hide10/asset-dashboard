"""サーバー内蔵スケジューラのテスト — 時刻判定・重複スキップ・設定保存を検証。"""

from datetime import datetime, timedelta

import pytest

import src.web.server as server
from src.db.repository import get_setting, save_setting
from src.db.schema import init_db
from src.web.server import (
    _next_scheduled_run,
    _parse_scheduler_time,
    _scheduler_tick,
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


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "scheduler_test.db"
    conn = init_db(str(p))
    conn.close()
    return str(p)


def _get(db_path: str, key: str) -> str | None:
    conn = init_db(db_path)
    try:
        return get_setting(conn, key)
    finally:
        conn.close()


def _set(db_path: str, key: str, value: str) -> None:
    conn = init_db(db_path)
    try:
        save_setting(conn, key, value)
    finally:
        conn.close()


class TestSchedulerTick:
    NOW = datetime(2026, 6, 10, 7, 5)

    def _stub_worker(self, monkeypatch, db_path: str, success: bool = True):
        """_run_update_locked を呼び出し記録付きのスタブに差し替える。"""
        calls = []

        def fake_worker(path):
            calls.append(path)
            if success:
                _set(db_path, "last_fetch_at", self.NOW.isoformat())

        monkeypatch.setattr(server, "_run_update_locked", fake_worker)
        return calls

    def test_disabled_does_nothing(self, monkeypatch, db_path):
        _set(db_path, "scheduler_enabled", "0")
        calls = self._stub_worker(monkeypatch, db_path)
        _scheduler_tick(db_path, now=self.NOW)
        assert calls == []
        assert _get(db_path, "scheduler_last_run_at") is None

    def test_before_time_does_nothing(self, monkeypatch, db_path):
        calls = self._stub_worker(monkeypatch, db_path)
        _scheduler_tick(db_path, now=datetime(2026, 6, 10, 6, 0))
        assert calls == []
        assert _get(db_path, "scheduler_last_run_at") is None

    def test_runs_and_records_success(self, monkeypatch, db_path):
        _set(db_path, "last_fetch_at", (self.NOW - timedelta(days=1)).isoformat())
        calls = self._stub_worker(monkeypatch, db_path, success=True)
        _scheduler_tick(db_path, now=self.NOW)
        assert calls == [db_path]
        assert _get(db_path, "scheduler_last_run_at") == self.NOW.isoformat()
        assert _get(db_path, "scheduler_last_result") == "success"

    def test_records_failure_when_fetch_not_updated(self, monkeypatch, db_path):
        _set(db_path, "last_fetch_at", (self.NOW - timedelta(days=1)).isoformat())
        calls = self._stub_worker(monkeypatch, db_path, success=False)
        _scheduler_tick(db_path, now=self.NOW)
        assert calls == [db_path]
        assert _get(db_path, "scheduler_last_result") == "failure"

    def test_no_duplicate_run_same_day(self, monkeypatch, db_path):
        _set(db_path, "last_fetch_at", (self.NOW - timedelta(days=1)).isoformat())
        calls = self._stub_worker(monkeypatch, db_path, success=True)
        _scheduler_tick(db_path, now=self.NOW)
        _scheduler_tick(db_path, now=datetime(2026, 6, 10, 8, 0))
        assert calls == [db_path]  # 2回目は重複スキップで呼ばれない

    def test_skipped_when_data_fresh(self, monkeypatch, db_path):
        # 起動時更新が直前（10分前）に成功済み → 試行は記録されるが取得はスキップ
        # _should_update は実時刻を参照するためスタブで「データが新しい」状態を固定する
        _set(db_path, "last_fetch_at", (self.NOW - timedelta(minutes=10)).isoformat())
        calls = self._stub_worker(monkeypatch, db_path)
        monkeypatch.setattr(server, "_should_update", lambda *a, **kw: False)
        _scheduler_tick(db_path, now=self.NOW)
        assert calls == []
        assert _get(db_path, "scheduler_last_run_at") == self.NOW.isoformat()
        assert _get(db_path, "scheduler_last_result") == "skipped"

    def test_carry_over_while_startup_update_running(self, monkeypatch, db_path):
        # 起動時更新がロックを保持中は試行時刻を保存せず持ち越す
        # → 起動時更新が失敗しても当日の再取得機会を失わない
        _set(db_path, "last_fetch_at", (self.NOW - timedelta(days=1)).isoformat())
        calls = self._stub_worker(monkeypatch, db_path, success=True)
        server._update_lock.acquire()
        try:
            _scheduler_tick(db_path, now=self.NOW)
        finally:
            server._update_lock.release()
        assert calls == []
        assert _get(db_path, "scheduler_last_run_at") is None  # 持ち越し
        # 起動時更新が終了（失敗で last_fetch_at は古いまま）→ 次の tick で実行される
        _scheduler_tick(db_path, now=datetime(2026, 6, 10, 7, 6))
        assert calls == [db_path]

    def test_custom_time_setting(self, monkeypatch, db_path):
        _set(db_path, "scheduler_time", "21:30")
        _set(db_path, "last_fetch_at", (self.NOW - timedelta(days=1)).isoformat())
        calls = self._stub_worker(monkeypatch, db_path)
        _scheduler_tick(db_path, now=datetime(2026, 6, 10, 21, 29))
        assert calls == []
        _scheduler_tick(db_path, now=datetime(2026, 6, 10, 21, 31))
        assert calls == [db_path]
