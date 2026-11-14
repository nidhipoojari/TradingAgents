#!/usr/bin/env python3
"""E3 tracing overhead, in process, with no LLM or network calls.

Times a fake chat model invoked through prompt | llm (as the agents do), 10 calls
per "run" (the Phase 2 call count), with no callbacks vs the E3 callbacks
(E3MetricsCallback + Langfuse CallbackHandler). Langfuse is pointed at an
unreachable host so no traces leave the machine; its batch export runs in a
background thread and is not on the calls' path. Reports the median added time
per run. This is the tracing code's own cost, not network export or flush time.
"""
import os
import statistics
import sys
import time

os.environ.update(LANGFUSE_PUBLIC_KEY="pk-offline", LANGFUSE_SECRET_KEY="sk-offline",
                  LANGFUSE_BASE_URL="http://127.0.0.1:9", OTEL_SDK_DISABLED="false")

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402

from tradingagents.e3_tracing import E3MetricsCallback  # noqa: E402

CALLS_PER_RUN, RUNS = 10, int(sys.argv[1]) if len(sys.argv) > 1 else 50
reply = AIMessage(content="x" * 2000, usage_metadata={"input_tokens": 2600, "output_tokens": 520, "total_tokens": 3120})
model = FakeMessagesListChatModel(responses=[reply])
chain = ChatPromptTemplate.from_messages([("system", "s" * 8000), ("human", "{q}")]) | model


def one_run(callbacks):
    began = time.perf_counter()
    for i in range(CALLS_PER_RUN):
        chain.invoke({"q": "go"}, config={"callbacks": callbacks, "metadata": {"langgraph_node": f"Agent {i}"}})
    return time.perf_counter() - began


def main():
    from langfuse.langchain import CallbackHandler
    handler = CallbackHandler()
    for _ in range(5):  # warm-up both paths
        one_run([]), one_run([handler, E3MetricsCallback()])
    off, on = [], []
    for _ in range(RUNS):  # interleaved so drift hits both arms equally
        off.append(one_run([]))
        on.append(one_run([handler, E3MetricsCallback()]))
    delta_ms = (statistics.median(on) - statistics.median(off)) * 1000
    print(f"runs per arm: {RUNS}, LLM calls per run: {CALLS_PER_RUN}")
    print(f"off median: {statistics.median(off) * 1000:.2f} ms/run")
    print(f"on  median: {statistics.median(on) * 1000:.2f} ms/run")
    print(f"added by tracing: {delta_ms:.2f} ms/run")
    for wall in (26.825, 29.635, 30.201):
        print(f"  = {100 * delta_ms / 1000 / wall:.3f}% of a {wall} s Phase 2 run")
    sys.stdout.flush()
    os._exit(0)  # skip Langfuse's exit flush to the unreachable host


if __name__ == "__main__":
    main()
