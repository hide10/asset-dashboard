"""ポートフォリオ参照API (#86) の受け入れテスト。"""

import json
from io import BytesIO

from src.db.schema import init_db
from src.web.server import Handler, _get_portfolio_context


def _build_test_db(tmp_path, *, with_snapshot: bool = True) -> str:
    db_path = tmp_path / "portfolio-context.db"
    conn = init_db(str(db_path))
    if with_snapshot:
        conn.execute(
            "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
            ("2026-01-15", 8_000_000, json.dumps({"預金・現金": 3_000_000, "株式（現物）": 5_000_000}), "fixture"),
        )
        conn.execute(
            "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
            ("2026-01-16", 8_200_000, json.dumps({"預金・現金": 3_100_000, "株式（現物）": 5_100_000}), "fixture"),
        )
        conn.executemany(
            """
            INSERT INTO snapshot_holdings
                (date, symbol_or_code, name, quantity, value, asset_class, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-01-15", "1111", "架空旧工業", 10, 1_000_000, "株式（現物）", "旧口座"),
                ("2026-01-16", "2222", "架空電機", 20, 2_100_000, "株式（現物）", "証券口座A"),
                ("2026-01-16", "F001", "架空世界投信", 1, 3_000_000, "投資信託", "証券口座B"),
            ],
        )
        conn.commit()
    conn.close()
    return str(db_path)


def test_portfolio_context_uses_latest_snapshot_and_omits_accounts(tmp_path):
    context = _get_portfolio_context(_build_test_db(tmp_path))

    assert context["as_of"] == "2026-01-16"
    assert context["total_asset"] == 8_200_000
    assert context["by_asset_class"] == {"預金・現金": 3_100_000, "株式（現物）": 5_100_000}
    assert [item["code"] for item in context["holdings"]] == ["2222", "F001"]
    assert all("account" not in item and "position" not in item for item in context["holdings"])
    assert "架空旧工業" not in json.dumps(context, ensure_ascii=False)
    assert "sector_totals" in context


def _invoke_portfolio_endpoint(
    db_path: str,
    *,
    configured_token: str = "test-shared-token",  # noqa: S107 - 合成テスト値
    request_token: str | None = "test-shared-token",  # noqa: S107 - 合成テスト値
) -> tuple[int, dict[str, str], dict]:
    handler = Handler.__new__(Handler)
    handler.path = "/api/portfolio-context"
    handler.db_path = db_path
    handler.demo = False
    handler.portfolio_api_token = configured_token
    handler.headers = {"Authorization": f"Bearer {request_token}"} if request_token else {}
    handler.wfile = BytesIO()
    response: dict = {"status": None, "headers": {}}
    handler.send_response = lambda status: response.update(status=status)
    handler.send_header = lambda key, value: response["headers"].update({key: value})
    handler.end_headers = lambda: None

    handler.do_GET()
    return response["status"], response["headers"], json.loads(handler.wfile.getvalue())


def test_portfolio_context_endpoint_is_private_and_not_cached(tmp_path):
    status, headers, payload = _invoke_portfolio_endpoint(_build_test_db(tmp_path))

    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    assert payload["as_of"] == "2026-01-16"


def test_portfolio_context_endpoint_returns_404_without_data(tmp_path):
    status, headers, payload = _invoke_portfolio_endpoint(_build_test_db(tmp_path, with_snapshot=False))

    assert status == 404
    assert headers["Cache-Control"] == "no-store"
    assert payload == {"error": "portfolio data unavailable"}


def test_portfolio_context_endpoint_requires_bearer_token(tmp_path):
    db_path = _build_test_db(tmp_path)

    status, headers, payload = _invoke_portfolio_endpoint(db_path, request_token=None)

    assert status == 401
    assert headers["Cache-Control"] == "no-store"
    assert payload == {"error": "unauthorized"}


def test_portfolio_context_endpoint_is_disabled_without_server_token(tmp_path):
    db_path = _build_test_db(tmp_path)

    status, headers, payload = _invoke_portfolio_endpoint(db_path, configured_token="")

    assert status == 503
    assert headers["Cache-Control"] == "no-store"
    assert payload == {"error": "portfolio api is not configured"}
