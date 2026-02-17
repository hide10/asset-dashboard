"""repository.py のテスト — DB操作の正確性を検証。"""

import pytest

from src.db.repository import (
    get_cf_actual_savings,
    get_cf_available_months,
    get_cf_category_summary,
    get_cf_category_trend,
    get_cf_dividend_history,
    get_cf_fixed_expenses,
    get_cf_income_breakdown,
    get_cf_income_trend,
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
