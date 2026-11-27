"""F2: only argument validation is reported as "invalid request"; a failure inside the run keeps its run ID,
category and partial metrics, including a ValueError relayed from the worker process (offline)."""

import asyncio
import json
import re

import pytest

from tests.test_failed_run_reporting import worker_env  # noqa: F401
from tradingagents import e1_routing, mcp_server, service, worker

PAST = "2026-01-09"


def _tool_error(run, args=None):
    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    async def go():
        async with fastmcp.Client(mcp_server.create_server(run=run)) as client:
            await client.call_tool("analyze_stock", args or {"ticker": "NVDA", "trade_date": PAST,
                                                            "analysts": ["market"]})
    with pytest.raises(ToolError) as caught:
        asyncio.run(go())
    return str(caught.value)


@pytest.mark.unit
def test_bad_arguments_are_still_an_invalid_request():
    text = _tool_error(lambda *a, **k: pytest.fail("must not run"), {"ticker": "NV DA"})
    assert text.startswith("invalid request: ")


@pytest.mark.unit
def test_a_value_error_inside_the_run_is_a_run_failure_with_its_run_id():
    def run(request, on_event=None, cancel=None):
        raise ValueError("unexpected value inside the run")

    text = _tool_error(run)
    assert re.match(r"analysis failed \[invalid_request\] run \d{8}-\d{6}-[0-9a-f]{8}: ", text), text
    assert not text.startswith("invalid request")


def _through_worker(worker_env, setup):  # noqa: F811
    from tests.test_worker import _request

    def run(request, on_event=None, cancel=None):
        real = _request(worker_env, analysts=request.analysts)
        real = service.AnalysisRequest(**{**real.__dict__, "run_id": request.run_id, "budget": request.budget})
        return worker.run_in_worker(real, on_event=on_event, cancel=cancel, setup=setup)
    return run


@pytest.mark.unit
def test_a_worker_relayed_value_error_keeps_its_record(worker_env):  # noqa: F811
    text = _tool_error(_through_worker(worker_env, "tests.worker_fakes:value_error_after_two_calls"))
    assert re.match(r"analysis failed \[invalid_request\] run \d{8}-\d{6}-[0-9a-f]{8}: ", text), text
    partial = json.loads(text.split("partial metrics: ", 1)[1])
    assert partial["llm_calls_succeeded"] == 2


@pytest.mark.unit
def test_a_configuration_error_keeps_its_category_across_the_worker(worker_env):  # noqa: F811
    from tests.test_worker import _request

    with pytest.raises(e1_routing.FallbackConfigError) as caught:
        worker.run_in_worker(_request(worker_env, analysts=("market",)),
                             setup="tests.worker_fakes:config_error_after_two_calls")
    assert service.error_category(caught.value) == "configuration"
    assert caught.value.analysis_failure["llm_calls_succeeded"] == 2
    text = mcp_server.failure_message(caught.value, "r")
    assert text.startswith("analysis failed [configuration] run ") and "ANTHROPIC_API_KEY" in text
