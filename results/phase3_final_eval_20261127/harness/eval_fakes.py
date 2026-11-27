"""Extra worker fakes for this evaluation (offline). Imported in the worker child by `module:function`."""
import os

from tests import worker_fakes


def slow_2s() -> None:
    """Scripted models taking 2 s per call (about 20 s per run): long enough to test busy and timeout."""
    worker_fakes._offline_vendors()
    worker_fakes._models(worker_fakes._scripted(slow=2.0))


ERRORS = {
    # Texts in the providers' documented formats (as tests/test_e1_quota_and_providers.py models them).
    "429": lambda: RuntimeError("429 RESOURCE_EXHAUSTED: Resource has been exhausted (e.g. check quota). "
                                "Please retry in 7s."),
    # Gemini's real per-day quota error, as the unit tests' fixture (QuotaFailure details with a quoted quotaId).
    "quota": lambda: RuntimeError(__import__("tests.test_e1_quota_and_providers", fromlist=["x"]).GEMINI_DAILY),
    "503": lambda: RuntimeError("503 UNAVAILABLE: This model is currently experiencing high demand."),
    "network": lambda: __import__("httpx").ConnectError("[Errno -3] Temporary failure in name resolution"),
}


def faults() -> None:
    """Scripted models and vendors whose failures are chosen per request by the environment:
    EVAL_MAIN / EVAL_BACKUP: ok | 429 | quota | 503 | network (every call of that model fails);
    EVAL_VENDOR: ok | network (every market-data vendor raises a connection error)."""
    from tests.test_graph_end_to_end import _Client
    from tradingagents import e1_routing
    from tradingagents.dataflows import router

    worker_fakes._offline_vendors()
    healthy = {m: dict(v) for m, v in router.VENDOR_METHODS.items()}

    def vendor(method, name):
        def call(*a, **k):
            mode = os.environ.get("EVAL_VENDOR", "ok")
            if mode == "network":
                # What the real Yahoo wrapper raises for any failed request (vendors/yahoo/common.py yf_retry).
                from tradingagents.dataflows.errors import VendorUnavailableError
                raise VendorUnavailableError("Yahoo Finance request failed: ConnectionError")
            if mode == "raw":
                raise ConnectionError("[Errno 101] Network is unreachable")   # an untyped error at the router
            return healthy[method][name](*a, **k)
        return call

    for method, vendors in router.VENDOR_METHODS.items():
        for name in list(vendors):
            vendors[name] = vendor(method, name)

    base = type(worker_fakes._scripted())

    class Faulty(base):
        role: str = "main"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            mode = os.environ.get(f"EVAL_{self.role.upper()}", "ok")
            if mode != "ok":
                raise ERRORS[mode]()
            return super()._generate(messages, stop, run_manager, **kwargs)

    main, backup = Faulty(model_name="gemini-test"), Faulty(model_name="gemini-test-backup", role="backup")
    e1_routing.create_tier_client = lambda cfg, tier, **k: _Client(
        (backup if cfg[f"{tier}_think_llm"] == "gemini-test-backup" else main).model_copy(
            update={"callbacks": k.get("callbacks")}))
