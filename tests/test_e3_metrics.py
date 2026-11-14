"""Offline regression tests for E3 metrics and cost reporting."""

from tradingagents.e3_tracing import E3MetricsCallback


def test_partial_cost_when_primary_model_fails():
    callback = E3MetricsCallback()
    callback.records = [
        {
            "run_id": "successful-fallback",
            "agent": "Neutral Analyst",
            "model": "gemini-3.1-flash-lite",
            "latency_ms": 1500,
            "input_tokens": 1000,
            "output_tokens": 100,
            "error": None,
        },
        {
            "run_id": "failed-primary",
            "agent": "Neutral Analyst",
            "model": "gemini-3.5-flash-lite",
            "latency_ms": 500,
            "input_tokens": None,
            "output_tokens": None,
            "error": "HTTP 429 RESOURCE_EXHAUSTED",
        },
    ]

    pricing = {
        "gemini-3.1-flash-lite": {
            "input_per_million_usd": 0.25,
            "output_per_million_usd": 1.50,
        },
        "gemini-3.5-flash-lite": {
            "input_per_million_usd": 0.30,
            "output_per_million_usd": 2.50,
        },
    }

    result = callback.summary(
        ticker="NVDA",
        analysis_date="2026-10-23",
        status="success",
        wall_time_seconds=10.0,
        pricing=pricing,
    )

    assert result["estimated_cost_usd"] is None
    assert result["known_usage_cost_usd"] == 0.0004
    assert result["cost_complete"] is False
    assert result["priced_llm_calls"] == 1
    assert result["unpriced_llm_calls"] == 1
    assert result["observed_llm_errors"] == 1


def test_market_analyst_attribution():
    callback = E3MetricsCallback()

    callback._start(
        serialized={"kwargs": {"model": "gemini-3.5-flash-lite"}},
        run_id="market-call",
        metadata={"langgraph_node": "Market Analyst"},
    )
    callback._finish(
        run_id="market-call",
        input_tokens=1000,
        output_tokens=100,
    )

    result = callback.summary(
        ticker="NVDA",
        analysis_date="2026-10-23",
        status="success",
        wall_time_seconds=1.0,
    )

    assert "Market Analyst" in result["per_agent"]
    assert "agent" not in result["per_agent"]
    assert result["observed_llm_calls"] == 1


def test_per_call_metrics_are_exported():
    callback = E3MetricsCallback()

    callback._start(
        serialized={"kwargs": {"model": "gemini-3.5-flash-lite"}},
        run_id="market-call-1",
        metadata={"langgraph_node": "Market Analyst"},
    )

    callback._finish(
        run_id="market-call-1",
        input_tokens=1000,
        output_tokens=100,
    )

    result = callback.summary(
        ticker="NVDA",
        analysis_date="2026-10-23",
        status="success",
        wall_time_seconds=1.0,
    )

    assert len(result["per_call"]) == 1
    assert result["per_call"][0]["agent"] == "Market Analyst"
    assert result["per_call"][0]["input_tokens"] == 1000
    assert result["per_call"][0]["output_tokens"] == 100
    assert result["per_call"][0]["had_error"] is False


def test_e3_session_save_and_langfuse_flush(monkeypatch, tmp_path):
    """Offline integration test for E3 session creation and persistence."""
    import json
    import sys
    import types

    from tradingagents.e3_cli_hook import E3Session

    class FakeClient:
        def __init__(self):
            self.flush_count = 0

        def flush(self):
            self.flush_count += 1

    class FakeHandler:
        pass

    client = FakeClient()

    fake_langfuse = types.ModuleType("langfuse")
    fake_langfuse.__path__ = []
    fake_langfuse.get_client = lambda: client

    fake_langchain = types.ModuleType("langfuse.langchain")
    fake_langchain.CallbackHandler = FakeHandler

    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_langchain)

    monkeypatch.setenv("E3_TRACE_ENABLED", "1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "offline-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "offline-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost.invalid")
    monkeypatch.setenv("E3_RESULTS_DIR", str(tmp_path))
    monkeypatch.delenv("E3_PRICING_JSON", raising=False)

    session = E3Session.from_env("NVDA", "2026-10-23")

    metrics = next(
        callback for callback in session.callbacks
        if isinstance(callback, E3MetricsCallback)
    )

    for i in range(12):
        run_id = f"simulated-{i}"
        metrics._start(
            {"kwargs": {"model": "gemini-3.5-flash-lite"}},
            run_id,
            {"langgraph_node": "Market Analyst"},
        )
        metrics._finish(
            run_id,
            input_tokens=1000,
            output_tokens=200,
        )

    session.mark_completed("Hold")
    session.save()

    files = list(tmp_path.glob("*_e3.json"))
    assert len(files) == 1

    report = json.loads(files[0].read_text())

    assert report["status"] == "success"
    assert report["observed_llm_calls"] == 12
    assert report["observed_llm_errors"] == 0
    assert report["decision"] == "Hold"
    assert client.flush_count == 1

    session.save()

    assert len(list(tmp_path.glob("*_e3.json"))) == 1
    assert client.flush_count == 1


def test_timeline_separates_llm_time_from_gaps():
    callback = E3MetricsCallback()
    base = {"agent": "Market Analyst", "model": "m", "input_tokens": 1,
            "output_tokens": 1, "error": None}
    callback.records = [
        {**base, "run_id": "a", "start_offset_ms": 2000.0, "latency_ms": 1000.0},
        {**base, "run_id": "b", "start_offset_ms": 4000.0, "latency_ms": 2000.0},
        # Overlaps b: counted once in busy time, twice in summed latency.
        {**base, "run_id": "c", "start_offset_ms": 5000.0, "latency_ms": 2000.0},
    ]

    result = callback.summary(ticker="NVDA", analysis_date="2026-10-23",
                              status="success", wall_time_seconds=10.0)

    assert result["timing"] == {
        "llm_busy_seconds": 4.0,
        "summed_llm_latency_seconds": 5.0,
        "before_first_llm_seconds": 2.0,
        "between_llm_calls_seconds": 1.0,
        "after_last_llm_seconds": 3.0,
    }
    assert result["per_call"][0]["start_offset_ms"] == 2000.0


def test_timeline_absent_for_records_without_offsets():
    callback = E3MetricsCallback()
    callback.records = [{"run_id": "a", "agent": "x", "model": "m", "latency_ms": 5.0,
                         "input_tokens": 1, "output_tokens": 1, "error": None}]

    result = callback.summary(ticker="NVDA", analysis_date="2026-10-23",
                              status="success", wall_time_seconds=1.0)

    assert result["timing"] is None
    assert result["per_call"][0]["start_offset_ms"] is None


def test_recorded_calls_carry_start_offsets():
    callback = E3MetricsCallback()
    callback._start({"kwargs": {"model": "m"}}, "r", {"langgraph_node": "Trader"})
    callback._finish("r", input_tokens=1, output_tokens=1)

    assert callback.records[0]["start_offset_ms"] >= 0
