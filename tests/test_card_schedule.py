from datetime import date

from src.parser.card_schedule import (
    CardAccountLink,
    parse_card_account_links,
    parse_card_schedule_html,
)


def test_parse_card_account_links_keeps_non_card_names_and_deduplicates():
    html = """
    <table>
      <tr><td><a href="/accounts/show/bank">銀行</a></td></tr>
      <tr><td><a href="/accounts/show/card">三井住友</a></td></tr>
      <tr><td><a href="/accounts/show/card">三井住友</a></td></tr>
    </table>
    """

    assert parse_card_account_links(html) == [
        CardAccountLink(name="銀行", href="/accounts/show/bank"),
        CardAccountLink(name="三井住友", href="/accounts/show/card"),
    ]


def test_parse_card_schedule_html_extracts_amount_and_due_date():
    account = CardAccountLink(name="東急カード", href="/accounts/show/card-1")
    html = """
    <table id="TABLE_1">
      <thead><tr><th>名称</th><th>種類</th><th>番号</th><th>引き落とし予定額</th></tr></thead>
      <tbody>
        <tr><td></td><td>TOKYU CARD</td><td></td><td>-12,345円 (2026/08/10)</td><td>-</td></tr>
        <tr><td>ご本人</td><td>TOKYU CARD</td><td></td><td>-</td><td>-</td></tr>
        <tr><td></td><td>ポイント</td><td></td><td>-9,999円 (2026/08/10)</td><td>-</td></tr>
      </tbody>
    </table>
    """

    payments = parse_card_schedule_html(html, account)

    assert len(payments) == 1
    assert payments[0].due_date == date(2026, 8, 10)
    assert payments[0].amount == 12_345
    assert payments[0].card_name == "東急カード / TOKYU CARD"
    assert payments[0].external_id == "/accounts/show/card-1#0"


def test_parse_card_schedule_html_ignores_unknown_or_non_schedule_tables():
    account = CardAccountLink(name="カード", href="/accounts/show/card")
    html = """
    <table><tr><th>名称</th><th>金額</th></tr><tr><td>カード</td><td>1,000円</td></tr></table>
    <table><tr><th>名称</th><th>引き落とし予定額</th></tr><tbody>
      <tr><td>カード</td><td>未確定</td></tr>
      <tr><td>カード</td><td>1,000円</td></tr>
    </tbody></table>
    """

    assert parse_card_schedule_html(html, account) == []
