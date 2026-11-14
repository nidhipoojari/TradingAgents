"""Offline tests for Market Analyst context compaction."""

from langchain_core.messages import AIMessage, ToolMessage

from tradingagents.agents.analysts.compact_context import (
    compact_market_messages,
)


def test_stock_data_compacted_without_mutation():
    csv = (
        "# Stock data for NVDA\n"
        "# Total records: 80\n\n"
        "Date,Open,High,Low,Close,Volume\n"
        + "\n".join(
            f"2026-01-{i:02d},100,105,95,102,1000000"
            for i in range(1, 81)
        )
    )

    call = AIMessage(
        content="",
        tool_calls=[{
            "name": "get_stock_data",
            "args": {},
            "id": "stock-1",
        }],
    )
    result = ToolMessage(
        content=csv,
        tool_call_id="stock-1",
        name="get_stock_data",
    )

    original = [call, result]
    compacted = compact_market_messages(original)

    assert len(compacted[1].content) < len(csv)
    assert "historical rows omitted from LLM context" in compacted[1].content
    assert compacted[1].tool_call_id == "stock-1"
    assert compacted[0].tool_calls == call.tool_calls
    assert result.content == csv
    assert original[1] is result


def test_indicator_history_compacted():
    content = (
        "## RSI values\n\n"
        + "\n".join(
            f"2026-10-{i:02d}: {50 + i}"
            for i in range(31, 0, -1)
        )
        + "\nRSI interpretation"
    )

    result = ToolMessage(
        content=content,
        tool_call_id="rsi-1",
        name="get_indicators",
    )

    compacted = compact_market_messages([result])[0]

    assert len(compacted.content) < len(content)
    assert "2026-10-31: 81" in compacted.content
    assert "2026-10-01: 51" in compacted.content
    assert "RSI interpretation" in compacted.content
    assert result.content == content


def test_verified_snapshot_preserved_exactly():
    content = "VERIFIED SNAPSHOT\n" + ("Precise data\n" * 100)

    result = ToolMessage(
        content=content,
        tool_call_id="verified-1",
        name="get_verified_market_snapshot",
    )

    compacted = compact_market_messages([result])

    assert compacted[0].content == content
    assert compacted[0].tool_call_id == result.tool_call_id


def test_significant_price_move_preserved_with_valid_dates():
    import pandas as pd

    dates = pd.bdate_range("2026-07-01", periods=60)
    rows = []

    for i, date in enumerate(dates):
        close = 150 if i == 30 else 100
        rows.append(
            f"{date:%Y-%m-%d},100,155,95,{close},1000000"
        )

    content = (
        "# Stock data for NVDA\n"
        "Date,Open,High,Low,Close,Volume\n"
        + "\n".join(rows)
    )

    original = ToolMessage(
        content=content,
        tool_call_id="stock-valid-dates",
        name="get_stock_data",
    )

    result = compact_market_messages([original])[0]

    assert rows[30] in result.content
    assert rows[29] in result.content
    assert len(result.content) < len(content)
    assert original.content == content
