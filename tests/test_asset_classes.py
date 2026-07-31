import json

from src.analysis.compare import compare_monthly
from src.db.repository import get_latest_stock_codes
from src.db.schema import init_db


def test_init_db_migrates_legacy_cash_class_in_snapshot_and_accounts(tmp_path):
    db_path = tmp_path / "assets.db"
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        ("2026-07-01", 1_000, json.dumps({"預金・現金・暗号資産": 600, "株式（現物）": 400}), ""),
    )
    conn.execute(
        "INSERT INTO snapshot_accounts (date, account_name, asset_class, balance, institution) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-01", "普通預金", "預金・現金・暗号資産", 600, "銀行"),
    )
    conn.commit()
    conn.close()

    conn = init_db(db_path)
    by_class = json.loads(conn.execute("SELECT by_class_json FROM snapshots").fetchone()[0])
    account_class = conn.execute("SELECT asset_class FROM snapshot_accounts").fetchone()[0]

    assert by_class == {"預金・現金": 600, "株式（現物）": 400}
    assert account_class == "預金・現金"
    conn.close()


def test_monthly_comparison_treats_legacy_and_current_cash_class_as_one(tmp_path):
    db_path = tmp_path / "assets.db"
    conn = init_db(db_path)
    conn.executemany(
        "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        [
            ("2026-07-01", 1_000, json.dumps({"預金・現金・暗号資産": 600, "株式（現物）": 400}), ""),
            ("2026-07-31", 1_100, json.dumps({"預金・現金": 650, "株式（現物）": 450}), ""),
        ],
    )
    conn.commit()
    conn.close()

    result = compare_monthly(str(db_path), "2026-07-31")

    assert result.by_class_diff == {"預金・現金": 50, "株式（現物）": 50}


def test_latest_stock_codes_uses_latest_snapshot_only(tmp_path):
    conn = init_db(tmp_path / "assets.db")
    conn.executemany(
        "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, '{}', '')",
        [("2026-07-30", 100), ("2026-07-31", 200)],
    )
    conn.executemany(
        """
        INSERT INTO snapshot_holdings
            (date, symbol_or_code, name, value, asset_class)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("2026-07-30", "1111", "旧銘柄", 100, "株式（現物）"),
            ("2026-07-31", "8316", "三井住友FG", 100, "株式（現物）"),
            ("2026-07-31", "8593", "三菱HCキャピタル", 100, "株式（現物）"),
            ("2026-07-31", "", "投資信託", 100, "投資信託"),
        ],
    )
    conn.commit()

    assert get_latest_stock_codes(conn) == ["8316", "8593"]
    conn.close()
