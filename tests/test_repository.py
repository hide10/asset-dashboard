"""repository.py のテスト — DB操作の正確性を検証。"""

import pytest

from src.db.repository import (
    _adjusted_closing_date,
    _fiscal_month_range,
    _japanese_holidays,
    get_cf_actual_savings,
    get_cf_available_months,
    get_cf_category_summary,
    get_cf_category_trend,
    get_cf_dividend_history,
    get_cf_fixed_expenses,
    get_cf_income_breakdown,
    get_cf_income_trend,
    get_cf_monthly_trend,
)
from src.db.schema import init_db


@pytest.fixture
def conn(tmp_path):
    """テスト用のインメモリDBを作成し、テストデータを投入。"""
    db_path = tmp_path / "test.db"
    c = init_db(str(db_path))
    # 3ヶ月分のテストデータを投入
    rows = [
        # 2025-10: 固定費 + 変動費 + 収入
        ("tx01", "2025-10", "2025-10-01", "家賃", -85000, "銀行A", "住宅", "家賃・地代", "", 0, 1, "2025-10-15"),
        ("tx02", "2025-10", "2025-10-05", "スーパー", -15000, "カードA", "食費", "食料品", "", 0, 1, "2025-10-15"),
        ("tx03", "2025-10", "2025-10-10", "外食", -8000, "カードA", "食費", "外食", "", 0, 1, "2025-10-15"),
        ("tx04", "2025-10", "2025-10-15", "携帯", -8800, "銀行A", "通信費", "携帯電話", "", 0, 1, "2025-10-15"),
        ("tx05", "2025-10", "2025-10-20", "保険", -12000, "銀行A", "保険", "生命保険", "", 0, 1, "2025-10-15"),
        ("tx06", "2025-10", "2025-10-25", "給与", 350000, "銀行A", "収入", "給与", "", 0, 1, "2025-10-15"),
        ("tx07", "2025-10", "2025-10-28", "副業", 30000, "銀行B", "収入", "副業", "", 0, 1, "2025-10-15"),
        # 振替（除外対象）
        ("tx08", "2025-10", "2025-10-03", "振替", -50000, "銀行A", "", "", "", 1, 1, "2025-10-15"),
        # 2025-11: 固定費ほぼ同額 + 変動
        ("tx11", "2025-11", "2025-11-01", "家賃", -85000, "銀行A", "住宅", "家賃・地代", "", 0, 1, "2025-11-15"),
        ("tx12", "2025-11", "2025-11-05", "スーパー", -18000, "カードA", "食費", "食料品", "", 0, 1, "2025-11-15"),
        ("tx13", "2025-11", "2025-11-10", "外食", -5000, "カードA", "食費", "外食", "", 0, 1, "2025-11-15"),
        ("tx14", "2025-11", "2025-11-15", "携帯", -9000, "銀行A", "通信費", "携帯電話", "", 0, 1, "2025-11-15"),
        ("tx15", "2025-11", "2025-11-20", "保険", -12000, "銀行A", "保険", "生命保険", "", 0, 1, "2025-11-15"),
        ("tx16", "2025-11", "2025-11-25", "給与", 355000, "銀行A", "収入", "給与", "", 0, 1, "2025-11-15"),
        ("tx17", "2025-11", "2025-11-28", "副業", 25000, "銀行B", "収入", "副業", "", 0, 1, "2025-11-15"),
        # 2025-12: 固定費同額 + 変動大
        ("tx21", "2025-12", "2025-12-01", "家賃", -85000, "銀行A", "住宅", "家賃・地代", "", 0, 1, "2025-12-15"),
        ("tx22", "2025-12", "2025-12-05", "スーパー", -22000, "カードA", "食費", "食料品", "", 0, 1, "2025-12-15"),
        ("tx23", "2025-12", "2025-12-10", "旅行", -50000, "カードA", "趣味・娯楽", "旅行", "", 0, 1, "2025-12-15"),
        ("tx24", "2025-12", "2025-12-15", "携帯", -8500, "銀行A", "通信費", "携帯電話", "", 0, 1, "2025-12-15"),
        ("tx25", "2025-12", "2025-12-20", "保険", -12000, "銀行A", "保険", "生命保険", "", 0, 1, "2025-12-15"),
        ("tx26", "2025-12", "2025-12-25", "給与", 360000, "銀行A", "収入", "給与", "", 0, 1, "2025-12-15"),
        # 配当・分配金
        ("tx31", "2025-10", "2025-10-15", "住友商事 配当", 12500, "証券A", "収入", "配当金", "", 0, 1, "2025-10-15"),
        ("tx32", "2025-12", "2025-12-10", "ETF分配金", 8200, "証券A", "収入", "分配金", "", 0, 1, "2025-12-15"),
        ("tx33", "2025-12", "2025-12-20", "定期預金利息", 1500, "銀行B", "収入", "利息", "", 0, 1, "2025-12-15"),
    ]
    c.executemany(
        """INSERT INTO cf_transactions
           (id, year_month, date, description, amount, institution,
            major_category, minor_category, memo, is_transfer, is_target, fetched)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    c.commit()
    yield c
    c.close()


class TestCfCategorySummary:
    def test_basic(self, conn):
        result = get_cf_category_summary(conn, "2025-10")
        assert result["year_month"] == "2025-10"
        assert result["total_income"] == 392500  # 給与350000+副業30000+配当12500
        # 支出: 85000+15000+8000+8800+12000 = 128800
        assert result["total_expense"] == 128800
        assert result["balance"] == 392500 - 128800

    def test_excludes_transfers(self, conn):
        result = get_cf_category_summary(conn, "2025-10")
        # 振替の50000は含まれない
        assert result["total_expense"] == 128800

    def test_top_expenses(self, conn):
        result = get_cf_category_summary(conn, "2025-10")
        assert len(result["top_expenses"]) <= 15
        assert result["top_expenses"][0]["amount"] == 85000  # 家賃が最高額


class TestCfCategoryTrend:
    def test_returns_all_months(self, conn):
        result = get_cf_category_trend(conn, months=6)
        assert result["year_months"] == ["2025-10", "2025-11", "2025-12"]

    def test_categories_sorted_by_total(self, conn):
        result = get_cf_category_trend(conn, months=6)
        cats = result["categories"]
        # 住宅(85000*3=255000) が最大
        assert cats[0] == "住宅"

    def test_by_month_values(self, conn):
        result = get_cf_category_trend(conn, months=6)
        assert result["by_month"]["2025-10"]["住宅"] == 85000
        assert result["by_month"]["2025-12"]["趣味・娯楽"] == 50000

    def test_empty_db(self, tmp_path):
        c = init_db(str(tmp_path / "empty.db"))
        result = get_cf_category_trend(c, months=6)
        assert result["year_months"] == []
        assert result["categories"] == []
        c.close()


class TestCfFixedExpenses:
    def test_detects_fixed_expenses(self, conn):
        result = get_cf_fixed_expenses(conn, months=3)
        fixed_pairs = [(f["major"], f["minor"]) for f in result["fixed"]]
        # 家賃(85000固定)、保険(12000固定)は固定費
        assert ("住宅", "家賃・地代") in fixed_pairs
        assert ("保険", "生命保険") in fixed_pairs

    def test_variable_not_in_fixed(self, conn):
        result = get_cf_fixed_expenses(conn, months=3)
        fixed_pairs = [(f["major"], f["minor"]) for f in result["fixed"]]
        # 旅行は12月のみ出現（1/3ヶ月）なので変動費
        assert ("趣味・娯楽", "旅行") not in fixed_pairs

    def test_fixed_ratio(self, conn):
        result = get_cf_fixed_expenses(conn, months=3)
        assert 0 < result["fixed_ratio"] < 100
        assert result["fixed_total"] > 0
        assert result["variable_total"] > 0
        assert result["months_used"] == 3

    def test_insufficient_data(self, tmp_path):
        """1ヶ月分のデータでは固定費判定不可。"""
        c = init_db(str(tmp_path / "short.db"))
        c.execute(
            """INSERT INTO cf_transactions
               (id, year_month, date, description, amount, institution,
                major_category, minor_category, memo, is_transfer, is_target, fetched)
               VALUES ('x1','2025-12','2025-12-01','test',-1000,'','A','B','',0,1,'2025-12-15')"""
        )
        c.commit()
        result = get_cf_fixed_expenses(c, months=3)
        assert result["fixed"] == []
        assert result["months_used"] == 1
        c.close()


class TestCfIncomeBreakdown:
    def test_basic(self, conn):
        result = get_cf_income_breakdown(conn, "2025-10")
        names = [i["name"] for i in result["items"]]
        assert "給与" in names
        assert "副業" in names
        assert result["total"] == 392500  # 給与350000+副業30000+配当12500

    def test_no_side_income_month(self, conn):
        # 2025-12には副業がない（給与+分配金+利息）
        result = get_cf_income_breakdown(conn, "2025-12")
        assert result["total"] == 369700  # 給与360000+分配金8200+利息1500
        assert len(result["items"]) == 3


class TestCfIncomeTrend:
    def test_returns_months(self, conn):
        result = get_cf_income_trend(conn, months=6)
        assert len(result) == 3
        # 古い順
        assert result[0]["year_month"] == "2025-10"
        assert result[0]["income"] == 392500  # 給与350000+副業30000+配当12500

    def test_limit(self, conn):
        result = get_cf_income_trend(conn, months=2)
        assert len(result) == 2


class TestCfActualSavings:
    def test_basic(self, conn):
        result = get_cf_actual_savings(conn, months=6)
        assert result is not None
        assert result["months_used"] == 3
        assert result["avg_income"] > 0
        assert result["avg_expense"] > 0
        # 貯蓄 = 収入 - 支出（正のはず）
        assert result["avg_savings"] > 0
        assert 0 < result["savings_rate"] < 100

    def test_empty_db(self, tmp_path):
        c = init_db(str(tmp_path / "empty.db"))
        result = get_cf_actual_savings(c, months=6)
        assert result is None
        c.close()


class TestCfAvailableMonths:
    def test_returns_months_from_transactions(self, conn):
        """cf_csv_months がなくても cf_transactions から月一覧を返す。"""
        result = get_cf_available_months(conn)
        months = [r["year_month"] for r in result]
        assert "2025-10" in months
        assert "2025-11" in months
        assert "2025-12" in months

    def test_has_data_flag(self, conn):
        """取引がある月は has_data=True。"""
        result = get_cf_available_months(conn)
        for r in result:
            assert r["has_data"] is True

    def test_fetched_from_transactions(self, conn):
        """cf_csv_months がなくても fetched が取引データから取得される。"""
        result = get_cf_available_months(conn)
        for r in result:
            assert r["fetched"] is not None
            assert r["fetched"] != ""

    def test_no_none_values(self, conn):
        """fetched と row_count に None がない。"""
        result = get_cf_available_months(conn)
        for r in result:
            assert r["fetched"] is not None
            assert r["row_count"] is not None
            assert r["row_count"] > 0

    def test_empty_db(self, tmp_path):
        c = init_db(str(tmp_path / "empty.db"))
        result = get_cf_available_months(c)
        assert result == []
        c.close()


class TestCfDividendHistory:
    def test_monthly(self, conn):
        result = get_cf_dividend_history(conn)
        monthly = result["monthly"]
        assert len(monthly) == 2  # 2025-10, 2025-12
        assert monthly[0]["year_month"] == "2025-10"
        assert monthly[0]["amount"] == 12500
        assert monthly[1]["year_month"] == "2025-12"
        assert monthly[1]["amount"] == 8200 + 1500  # 分配金 + 利息

    def test_annual(self, conn):
        result = get_cf_dividend_history(conn)
        annual = result["annual"]
        assert len(annual) == 1
        assert annual[0]["year"] == "2025"
        assert annual[0]["amount"] == 12500 + 8200 + 1500

    def test_empty_db(self, tmp_path):
        c = init_db(str(tmp_path / "empty.db"))
        result = get_cf_dividend_history(c)
        assert result["monthly"] == []
        assert result["annual"] == []
        c.close()


class TestFiscalMonthRange:
    """_fiscal_month_range ヘルパーのテスト。"""

    def test_closing_day_1(self):
        start, end = _fiscal_month_range("2025-02", 1)
        assert start == "2025-02-01"
        assert end == "2025-02-28"

    def test_closing_day_1_leap_year(self):
        start, end = _fiscal_month_range("2024-02", 1)
        assert start == "2024-02-01"
        assert end == "2024-02-29"

    def test_closing_day_25(self):
        # 2月分 = 1/25〜2/24
        start, end = _fiscal_month_range("2025-02", 25)
        assert start == "2025-01-25"
        assert end == "2025-02-24"

    def test_closing_day_25_january(self):
        # 1月分 = 12/25〜1/24
        start, end = _fiscal_month_range("2025-01", 25)
        assert start == "2024-12-25"
        assert end == "2025-01-24"

    def test_closing_day_10(self):
        # 3月分 = 2/10〜3/9
        start, end = _fiscal_month_range("2025-03", 10)
        assert start == "2025-02-10"
        assert end == "2025-03-09"

    def test_malformed_input_returns_safe_fallback(self):
        """不正な year_month でもクラッシュしない。"""
        start, end = _fiscal_month_range("bad", 25)
        assert start == "1970-01-01"

    def test_empty_input_returns_safe_fallback(self):
        start, end = _fiscal_month_range("", 1)
        assert start == "1970-01-01"

    def test_none_input_returns_safe_fallback(self):
        start, end = _fiscal_month_range(None, 10)
        assert start == "1970-01-01"

    def test_invalid_month_returns_safe_fallback(self):
        start, end = _fiscal_month_range("2025-13", 25)
        assert start == "1970-01-01"


class TestClosingDay:
    """締め日設定が各クエリに反映されるかテスト。"""

    @pytest.fixture
    def conn_closing(self, tmp_path):
        """締め日テスト用のデータ。

        締め日25日の場合:
        - 10/1〜10/24 → fiscal month 2025-10
        - 10/25〜11/24 → fiscal month 2025-11
        """
        db_path = tmp_path / "closing.db"
        c = init_db(str(db_path))
        rows = [
            # 10/1〜10/24: fiscal=2025-10 (closing_day=25)
            ("c01", "2025-10", "2025-10-01", "家賃10月前半", -80000, "銀行A", "住宅", "家賃", "", 0, 1, "2025-10-15"),
            (
                "c02",
                "2025-10",
                "2025-10-15",
                "スーパー10月前半",
                -10000,
                "カードA",
                "食費",
                "食料品",
                "",
                0,
                1,
                "2025-10-15",
            ),
            ("c03", "2025-10", "2025-10-20", "給与10月前半", 300000, "銀行A", "収入", "給与", "", 0, 1, "2025-10-15"),
            # 10/25〜11/24: fiscal=2025-11 (closing_day=25)
            ("c04", "2025-10", "2025-10-25", "携帯10月後半", -9000, "銀行A", "通信費", "携帯", "", 0, 1, "2025-10-15"),
            ("c05", "2025-10", "2025-10-28", "副業10月後半", 50000, "銀行B", "収入", "副業", "", 0, 1, "2025-10-15"),
            (
                "c06",
                "2025-11",
                "2025-11-05",
                "スーパー11月前半",
                -15000,
                "カードA",
                "食費",
                "食料品",
                "",
                0,
                1,
                "2025-11-15",
            ),
            ("c07", "2025-11", "2025-11-20", "給与11月", 310000, "銀行A", "収入", "給与", "", 0, 1, "2025-11-15"),
            # 11/25〜12/24: fiscal=2025-12 (closing_day=25)
            (
                "c08",
                "2025-11",
                "2025-11-25",
                "保険11月後半",
                -12000,
                "銀行A",
                "保険",
                "生命保険",
                "",
                0,
                1,
                "2025-11-15",
            ),
            (
                "c09",
                "2025-12",
                "2025-12-10",
                "スーパー12月前半",
                -20000,
                "カードA",
                "食費",
                "食料品",
                "",
                0,
                1,
                "2025-12-15",
            ),
            ("c10", "2025-12", "2025-12-15", "配当金", 5000, "証券A", "収入", "配当金", "", 0, 1, "2025-12-15"),
        ]
        c.executemany(
            """INSERT INTO cf_transactions
               (id, year_month, date, description, amount, institution,
                major_category, minor_category, memo, is_transfer, is_target, fetched)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        c.commit()
        yield c
        c.close()

    def test_summary_closing_day_1(self, conn_closing):
        """closing_day=1: 暦月の2025-10で集計。"""
        result = get_cf_category_summary(conn_closing, "2025-10", closing_day=1)
        # 10月の全取引: 支出80000+10000+9000=99000, 収入300000+50000=350000
        assert result["total_expense"] == 99000
        assert result["total_income"] == 350000

    def test_summary_closing_day_25(self, conn_closing):
        """closing_day=25: fiscal 2025-11 = 10/25〜11/24 で集計。"""
        result = get_cf_category_summary(conn_closing, "2025-11", closing_day=25)
        # 10/25: -9000, 10/28: +50000, 11/5: -15000, 11/20: +310000
        assert result["total_expense"] == 9000 + 15000  # 24000
        assert result["total_income"] == 50000 + 310000  # 360000

    def test_monthly_trend_closing_day_25(self, conn_closing):
        """closing_day=25: fiscal month ごとにグループ化。"""
        result = get_cf_monthly_trend(conn_closing, months=12, closing_day=25)
        # 3つの fiscal month が存在するはず
        months = [r["year_month"] for r in result]
        assert "2025-10" in months  # 10/1〜10/24
        assert "2025-11" in months  # 10/25〜11/24
        assert "2025-12" in months  # 11/25〜12/24

    def test_income_breakdown_closing_day_25(self, conn_closing):
        """closing_day=25: fiscal 2025-11 の収入内訳。"""
        result = get_cf_income_breakdown(conn_closing, "2025-11", closing_day=25)
        assert result["total"] == 360000  # 副業50000 + 給与310000

    def test_available_months_closing_day_25(self, conn_closing):
        """closing_day=25: fiscal month ベースの月一覧。"""
        result = get_cf_available_months(conn_closing, closing_day=25)
        months = [r["year_month"] for r in result]
        assert "2025-10" in months
        assert "2025-11" in months
        assert "2025-12" in months

    def test_dividend_history_closing_day_25(self, conn_closing):
        """closing_day=25: 配当金は fiscal 2025-12 に入る。"""
        result = get_cf_dividend_history(conn_closing, closing_day=25)
        monthly = result["monthly"]
        assert len(monthly) == 1
        assert monthly[0]["year_month"] == "2025-12"
        assert monthly[0]["amount"] == 5000

    def test_default_closing_day_backward_compatible(self, conn):
        """closing_day=1 (デフォルト) は既存テストと同じ結果。"""
        result = get_cf_category_summary(conn, "2025-10", closing_day=1)
        assert result["total_expense"] == 128800
        assert result["total_income"] == 392500


class TestJapaneseHolidays:
    """日本の祝日計算のテスト。"""

    def test_fixed_holidays_2026(self):
        from datetime import date

        holidays = _japanese_holidays(2026)
        assert date(2026, 1, 1) in holidays  # 元日
        assert date(2026, 2, 11) in holidays  # 建国記念の日
        assert date(2026, 2, 23) in holidays  # 天皇誕生日
        assert date(2026, 4, 29) in holidays  # 昭和の日
        assert date(2026, 5, 3) in holidays  # 憲法記念日
        assert date(2026, 5, 4) in holidays  # みどりの日
        assert date(2026, 5, 5) in holidays  # こどもの日
        assert date(2026, 8, 11) in holidays  # 山の日
        assert date(2026, 11, 3) in holidays  # 文化の日
        assert date(2026, 11, 23) in holidays  # 勤労感謝の日

    def test_happy_monday_2026(self):
        from datetime import date

        holidays = _japanese_holidays(2026)
        assert date(2026, 1, 12) in holidays  # 成人の日（1月第2月曜）
        assert date(2026, 7, 20) in holidays  # 海の日（7月第3月曜）
        assert date(2026, 9, 21) in holidays  # 敬老の日（9月第3月曜）
        assert date(2026, 10, 12) in holidays  # スポーツの日（10月第2月曜）

    def test_equinox_2026(self):
        holidays = _japanese_holidays(2026)
        # 春分の日は3/20 or 3/21付近
        spring = [d for d in holidays if d.month == 3 and 19 <= d.day <= 22]
        assert len(spring) >= 1
        # 秋分の日は9/22 or 9/23付近
        autumn = [d for d in holidays if d.month == 9 and 21 <= d.day <= 24]
        assert len(autumn) >= 1

    def test_substitute_holiday(self):
        """祝日が日曜の場合、翌月曜が振替休日になる。"""
        from datetime import date

        # 2026-05-03 (憲法記念日) は日曜日
        assert date(2026, 5, 3).weekday() == 6  # 日曜
        holidays = _japanese_holidays(2026)
        # 5/3日曜 → 5/4月曜は既にみどりの日 → 5/5火曜はこどもの日 → 5/6水曜が振替
        assert date(2026, 5, 6) in holidays

    def test_no_weekdays_in_holidays_set(self):
        """祝日セットに通常の平日は含まれない。"""
        from datetime import date

        holidays = _japanese_holidays(2026)
        # 2026-03-02 (月曜、祝日でない)
        assert date(2026, 3, 2) not in holidays

    def test_mountain_day_not_before_2016(self):
        """山の日(8/11)は2016年施行。2015年には含まれない。"""
        from datetime import date

        assert date(2015, 8, 11) not in _japanese_holidays(2015)
        assert date(2016, 8, 11) in _japanese_holidays(2016)

    def test_emperor_birthday_not_before_2020(self):
        """天皇誕生日(2/23)は2020年施行。2019年には含まれない。"""
        from datetime import date

        assert date(2019, 2, 23) not in _japanese_holidays(2019)
        assert date(2020, 2, 23) in _japanese_holidays(2020)

    def test_heisei_emperor_birthday(self):
        """平成天皇誕生日(12/23)は〜2018年。2019年は空白。"""
        from datetime import date

        assert date(2018, 12, 23) in _japanese_holidays(2018)
        assert date(2019, 12, 23) not in _japanese_holidays(2019)

    def test_olympic_2020_exceptions(self):
        """2020年オリンピック特例: 海の日→7/23、スポーツの日→7/24、山の日→8/10。"""
        from datetime import date

        h = _japanese_holidays(2020)
        assert date(2020, 7, 23) in h  # 海の日（通常は第3月曜）
        assert date(2020, 7, 24) in h  # スポーツの日（通常は10月第2月曜）
        assert date(2020, 8, 10) in h  # 山の日（通常は8/11）
        assert date(2020, 8, 11) not in h  # 通常の山の日はなし
        # 通常のハッピーマンデー位置にはない
        assert date(2020, 10, 12) not in h  # スポーツの日の通常位置

    def test_olympic_2021_exceptions(self):
        """2021年オリンピック特例: 海の日→7/22、スポーツの日→7/23、山の日→8/8。"""
        from datetime import date

        h = _japanese_holidays(2021)
        assert date(2021, 7, 22) in h
        assert date(2021, 7, 23) in h
        assert date(2021, 8, 8) in h
        assert date(2021, 8, 11) not in h


class TestAdjustedClosingDate:
    """営業日調整のテスト。"""

    def test_none_mode_no_change(self):
        from datetime import date

        result = _adjusted_closing_date(2026, 2, 25, "none")
        assert result == date(2026, 2, 25)

    def test_before_mode_weekday(self):
        """平日なら変更なし。"""
        from datetime import date

        # 2026-02-25 は水曜日
        assert date(2026, 2, 25).weekday() == 2
        result = _adjusted_closing_date(2026, 2, 25, "before")
        assert result == date(2026, 2, 25)

    def test_before_mode_saturday(self):
        """土曜 → 前日の金曜に移動。"""
        from datetime import date

        # 2026-01-25 が日曜日なので、2026-04-25 を確認
        # 2026-04-25 は土曜日
        assert date(2026, 4, 25).weekday() == 5  # 土曜
        result = _adjusted_closing_date(2026, 4, 25, "before")
        assert result == date(2026, 4, 24)  # 金曜

    def test_before_mode_sunday(self):
        """日曜 → 前の金曜に移動。"""
        from datetime import date

        # 2026-01-25 は日曜日
        assert date(2026, 1, 25).weekday() == 6  # 日曜
        result = _adjusted_closing_date(2026, 1, 25, "before")
        assert result == date(2026, 1, 23)  # 金曜

    def test_after_mode_saturday(self):
        """土曜 → 次の月曜に移動。"""
        from datetime import date

        # 2026-04-25 は土曜日
        assert date(2026, 4, 25).weekday() == 5
        result = _adjusted_closing_date(2026, 4, 25, "after")
        assert result == date(2026, 4, 27)  # 月曜

    def test_before_mode_holiday(self):
        """祝日の場合も前の平日に移動。"""
        from datetime import date

        # 2026-02-11 は建国記念の日（水曜日）
        assert date(2026, 2, 11).weekday() == 2  # 水曜
        result = _adjusted_closing_date(2026, 2, 11, "before")
        assert result == date(2026, 2, 10)  # 火曜

    def test_closing_day_clamps_to_month_end(self):
        """2月に closing_day=30 の場合、28日に調整。"""
        from datetime import date

        result = _adjusted_closing_date(2026, 2, 30, "none")
        assert result == date(2026, 2, 28)


class TestFiscalMonthRangeHoliday:
    """holiday_mode 付き _fiscal_month_range のテスト。"""

    def test_holiday_mode_none_same_as_default(self):
        start_none, end_none = _fiscal_month_range("2026-02", 25, "none")
        start_def, end_def = _fiscal_month_range("2026-02", 25)
        assert start_none == start_def
        assert end_none == end_def

    def test_holiday_mode_before_adjusts_start(self):
        # 2026-01-25 は日曜 → before で 1/23 (金) に
        start, end = _fiscal_month_range("2026-02", 25, "before")
        assert start == "2026-01-23"  # 日曜→金曜

    def test_holiday_mode_after_adjusts_start(self):
        # 2026-01-25 は日曜 → after で 1/26 (月) に
        start, end = _fiscal_month_range("2026-02", 25, "after")
        assert start == "2026-01-26"  # 日曜→月曜

    def test_holiday_mode_closing_day_1_unaffected(self):
        """closing_day=1 は holiday_mode に関係なく暦月。"""
        start_n, end_n = _fiscal_month_range("2026-02", 1, "none")
        start_b, end_b = _fiscal_month_range("2026-02", 1, "before")
        assert start_n == start_b == "2026-02-01"
        assert end_n == end_b == "2026-02-28"


class TestHolidayModeQuery:
    """holiday_mode が DB クエリ結果に反映されるテスト。"""

    @pytest.fixture
    def conn_holiday(self, tmp_path):
        """祝日テスト用データ。2026-01-25 は日曜日。

        closing_day=25, holiday_mode="before" の場合:
        - fiscal 2026-02: 1/23(金)〜2/24(火)
        closing_day=25, holiday_mode="after" の場合:
        - fiscal 2026-02: 1/26(月)〜2/24(火)
        """
        db_path = tmp_path / "holiday.db"
        c = init_db(str(db_path))
        rows = [
            # 1/23 (金): before=fiscal 2026-02, after=fiscal 2026-01
            ("h01", "2026-01", "2026-01-23", "スーパー", -5000, "カードA", "食費", "食料品", "", 0, 1, "2026-01-25"),
            # 1/24 (土): before=fiscal 2026-02, after=fiscal 2026-01
            ("h02", "2026-01", "2026-01-24", "外食", -3000, "カードA", "食費", "外食", "", 0, 1, "2026-01-25"),
            # 1/25 (日): none=fiscal 2026-02, before=fiscal 2026-02, after=fiscal 2026-01
            ("h03", "2026-01", "2026-01-25", "コンビニ", -1000, "カードA", "食費", "食料品", "", 0, 1, "2026-01-25"),
            # 1/26 (月): 全モードで fiscal 2026-02
            ("h04", "2026-01", "2026-01-26", "給与", 300000, "銀行A", "収入", "給与", "", 0, 1, "2026-01-26"),
            # 2/10 (火): 全モードで fiscal 2026-02
            ("h05", "2026-02", "2026-02-10", "家賃", -80000, "銀行A", "住宅", "家賃", "", 0, 1, "2026-02-15"),
        ]
        c.executemany(
            """INSERT INTO cf_transactions
               (id, year_month, date, description, amount, institution,
                major_category, minor_category, memo, is_transfer, is_target, fetched)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        c.commit()
        yield c
        c.close()

    def test_summary_before_includes_boundary_day(self, conn_holiday):
        """before モードでは 1/23 のデータが fiscal 2026-02 に含まれる。"""
        result = get_cf_category_summary(conn_holiday, "2026-02", closing_day=25, holiday_mode="before")
        # 1/23(-5000) + 1/24(-3000) + 1/25(-1000) + 2/10(-80000) = 89000
        assert result["total_expense"] == 89000
        # 収入: 1/26(+300000)
        assert result["total_income"] == 300000

    def test_summary_after_excludes_before_boundary(self, conn_holiday):
        """after モードでは 1/23,1/24,1/25 のデータが fiscal 2026-01 に入る。"""
        result = get_cf_category_summary(conn_holiday, "2026-02", closing_day=25, holiday_mode="after")
        # fiscal 2026-02: 1/26〜2/24 → 1/26(+300000), 2/10(-80000)
        assert result["total_expense"] == 80000
        assert result["total_income"] == 300000

    def test_summary_none_standard_boundary(self, conn_holiday):
        """none モードでは 1/25 以降が fiscal 2026-02。"""
        result = get_cf_category_summary(conn_holiday, "2026-02", closing_day=25, holiday_mode="none")
        # fiscal 2026-02: 1/25〜2/24 → 1/25(-1000), 1/26(+300000), 2/10(-80000)
        assert result["total_expense"] == 81000
        assert result["total_income"] == 300000


class TestClosingDay31:
    """closing_day=31 で短い月（4月=30日, 2月=28日）のテスト。

    SQL式とPython側 (_fiscal_month_range) の一貫性を検証する。
    """

    @pytest.fixture
    def conn_day31(self, tmp_path):
        db_path = tmp_path / "day31.db"
        c = init_db(str(db_path))
        rows = [
            # 4/30: 4月最終日。closing_day=31 → min(31,30)=30 なので fiscal 2025-05
            ("d01", "2025-04", "2025-04-30", "月末支出", -10000, "カードA", "食費", "食料品", "", 0, 1, "2025-05-01"),
            # 4/29: closing_day=31 → day(29) < min(31,30) → fiscal 2025-04
            ("d02", "2025-04", "2025-04-29", "前日支出", -5000, "カードA", "食費", "外食", "", 0, 1, "2025-05-01"),
            # 5/15: 普通の日 → fiscal 2025-05
            ("d03", "2025-05", "2025-05-15", "5月支出", -8000, "カードA", "食費", "食料品", "", 0, 1, "2025-05-20"),
            # 2/28: 2月最終日。closing_day=31 → min(31,28)=28 なので fiscal 2025-03
            ("d04", "2025-02", "2025-02-28", "2月末支出", -7000, "カードA", "食費", "食料品", "", 0, 1, "2025-03-01"),
            # 2/27: fiscal 2025-02
            ("d05", "2025-02", "2025-02-27", "2月27日", -3000, "カードA", "食費", "外食", "", 0, 1, "2025-03-01"),
        ]
        c.executemany(
            """INSERT INTO cf_transactions
               (id, year_month, date, description, amount, institution,
                major_category, minor_category, memo, is_transfer, is_target, fetched)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        c.commit()
        yield c
        c.close()

    def test_trend_groups_april_30_into_may(self, conn_day31):
        """closing_day=31: 4/30 は fiscal 2025-05 にグループ化される。"""
        trend = get_cf_monthly_trend(conn_day31, months=12, closing_day=31)
        by_month = {t["year_month"]: t["expense"] for t in trend}
        # 4/30(-10000) は fiscal 2025-05 に、4/29(-5000) は fiscal 2025-04 に
        assert by_month.get("2025-04") == 5000
        assert by_month.get("2025-05") == 10000 + 8000

    def test_summary_consistent_with_trend(self, conn_day31):
        """summary (range ベース) と trend (SQL式) が一致する。"""
        summary = get_cf_category_summary(conn_day31, "2025-05", closing_day=31)
        trend = get_cf_monthly_trend(conn_day31, months=12, closing_day=31)
        trend_may = next(t for t in trend if t["year_month"] == "2025-05")
        assert summary["total_expense"] == trend_may["expense"]

    def test_feb_28_rolls_to_march(self, conn_day31):
        """closing_day=31: 2/28 は fiscal 2025-03 にグループ化される。"""
        trend = get_cf_monthly_trend(conn_day31, months=12, closing_day=31)
        by_month = {t["year_month"]: t["expense"] for t in trend}
        # 2/28(-7000) → fiscal 2025-03, 2/27(-3000) → fiscal 2025-02
        assert by_month.get("2025-02") == 3000
        assert by_month.get("2025-03") == 7000

    def test_range_consistent_with_sql(self, conn_day31):
        """_fiscal_month_range と SQL式の結果が一致する（4月/2月）。"""
        # fiscal 2025-05: range = 4/30〜5/30
        start, end = _fiscal_month_range("2025-05", 31)
        assert start == "2025-04-30"
        assert end == "2025-05-30"

        # fiscal 2025-03: range = 2/28〜3/30
        start, end = _fiscal_month_range("2025-03", 31)
        assert start == "2025-02-28"
        assert end == "2025-03-30"
