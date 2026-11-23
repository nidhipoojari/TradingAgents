"""The live smoke-test scripts, offline: they do nothing live without approval, and
their pass/fail checks pass on a correct run (the real graph with scripted models,
and a fake paper account), so a live FAIL means the live system, not the check."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_graph_end_to_end import offline  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "smoke" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gemini_smoke():
    return _load("live_gemini_langfuse")


@pytest.fixture
def alpaca_smoke():
    return _load("live_alpaca_paper")


# ------------------------------------------------------------------ nothing live without approval
@pytest.mark.unit
def test_gemini_smoke_only_prints_its_plan_without_approval(gemini_smoke, monkeypatch, capsys):
    from tradingagents import service

    monkeypatch.setattr(service, "run_analysis", lambda *a, **k: pytest.fail("no analysis without approval"))
    assert gemini_smoke.main([]) == 2
    out = capsys.readouterr().out
    assert "NOT RUN" in out and "langfuse.session" in out and "Pre-flight (offline)" in out


@pytest.mark.unit
def test_gemini_smoke_stops_on_a_failed_preflight_even_when_approved(gemini_smoke, monkeypatch, capsys):
    from tradingagents import service

    monkeypatch.setattr(service, "run_analysis", lambda *a, **k: pytest.fail("pre-flight must stop it"))
    monkeypatch.setenv("E3_TRACE_ENABLED", "")
    assert gemini_smoke.main(["--approve-live-llm"]) == 1
    assert "E3_TRACE_ENABLED=1 is not set" in capsys.readouterr().out


@pytest.mark.unit
def test_alpaca_smoke_only_prints_its_plan_without_approval(alpaca_smoke, monkeypatch, capsys):
    from tradingagents.execution import alpaca_paper as ap

    monkeypatch.setattr(ap.AlpacaPaperBroker, "from_env", classmethod(lambda cls, **k: pytest.fail("no client")))
    assert alpaca_smoke.main([]) == 2
    out = capsys.readouterr().out
    assert "NOT RUN" in out and "order.duplicate" in out


@pytest.mark.unit
def test_alpaca_smoke_stops_without_keys_even_when_approved(alpaca_smoke, monkeypatch, capsys):
    from tradingagents.execution import alpaca_paper as ap

    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ap.AlpacaPaperBroker, "from_env", classmethod(lambda cls, **k: pytest.fail("no client")))
    assert alpaca_smoke.main(["--approve-read"]) == 1
    assert "paper keys" in capsys.readouterr().out


@pytest.mark.unit
def test_an_order_needs_a_username_and_a_resting_limit(alpaca_smoke, monkeypatch, capsys):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert alpaca_smoke.main(["--approve-order", "--limit-fraction", "1.2"]) == 1
    out = capsys.readouterr().out
    assert "--username" in out and "rests unfilled" in out


# ------------------------------------------------------------------ the checks pass on a correct run
@pytest.mark.unit
def test_gemini_smoke_check_run_on_a_real_offline_result(gemini_smoke, tmp_path, monkeypatch, offline):  # noqa: F811
    from tests.test_e1_e2_e3_audit import _run

    result, _summary, _ = _run(tmp_path, monkeypatch, quick="gemini-quick", deep="gemini-deep")
    report = gemini_smoke.Report()
    usage = gemini_smoke.check_run(report, result, "run1", max_cost=0.05, deep_model="gemini-deep",
                                   force_fallback=False)
    assert not report.failed, report.rows
    assert usage["run_id"] == result.run_id
    # And it does fail a bad result: a cost ceiling below the run's cost.
    strict = gemini_smoke.Report()
    gemini_smoke.check_run(strict, result, "run1", max_cost=0.0, deep_model="gemini-deep", force_fallback=False)
    assert [n for n, s, _ in strict.rows if s == "FAIL"] == ["run1.e3.cost"]


@pytest.mark.unit
def test_gemini_smoke_check_run_on_a_forced_fallback(gemini_smoke, tmp_path, monkeypatch, offline):  # noqa: F811
    from tests.test_e1_e2_e3_audit import _run

    result, _summary, _ = _run(tmp_path, monkeypatch, broken=True)
    report = gemini_smoke.Report()
    gemini_smoke.check_run(report, result, "run1", max_cost=0.05, deep_model="deep-m", force_fallback=True)
    assert not report.failed, report.rows
    assert any(n == "run1.e1.fallback" and s == "PASS" for n, s, _ in report.rows)


class _PaperAccount:
    """FakeTrading plus what the order level uses: cancel and order status."""

    def __new__(cls):
        from tests.test_alpaca_paper import FakeTrading

        class Account(FakeTrading):
            def submit_order(self, order_data):
                placed = super().submit_order(order_data)
                placed.status, placed.filled_qty = "accepted", "0"
                return placed

            def cancel_order_by_id(self, order_id):
                for order in self.orders.values():
                    if order.id == order_id:
                        order.status = "canceled"

            def get_all_positions(self):
                return []

        return Account()


@pytest.mark.unit
def test_alpaca_smoke_checks_pass_on_a_correct_paper_account(alpaca_smoke, tmp_path, monkeypatch):
    from tests.test_alpaca_paper import FakeData
    from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op

    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"alice": auth.hash_password("correct horse battery")}))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(users))
    monkeypatch.setattr(alpaca_smoke.getpass, "getpass", lambda prompt="": "correct horse battery")
    monkeypatch.setattr("builtins.input", lambda prompt="": "buy 1 F at 5.00")
    broker = ap.AlpacaPaperBroker(_PaperAccount(), FakeData(price=10.0),
                                  ledger=ap.OrderLedger(tmp_path / ap.LEDGER_NAME),
                                  sessions=auth.SessionStore(tmp_path / "auth.sqlite"))   # as --approve-order does
    limits = op.OrderLimits(1000.0)
    report = alpaca_smoke.Report()

    account, clock, price, held = alpaca_smoke.read_checks(report, broker, "F", limits)
    alpaca_smoke.order_checks(report, broker, "F", limits, account, clock, price, held, "alice", 0.5)

    assert not report.failed, report.rows
    names = [n for n, _, _ in report.rows]
    assert names[:8] == ["read.endpoint", "read.account", "read.clock", "read.price", "read.positions",
                         "local.cap", "local.market", "local.ledger"]
    assert {"order.parameters", "order.submitted", "order.ledger", "order.duplicate", "order.cancel_request",
            "order.final_status", "order.unfilled"} <= set(names)
    [sent] = broker.trading.submitted
    assert (sent.type.value, sent.limit_price, sent.qty) == ("limit", 5.0, 1.0)


@pytest.mark.unit
def test_alpaca_smoke_sends_nothing_unless_the_order_is_typed_back(alpaca_smoke, tmp_path, monkeypatch):
    from tests.test_alpaca_paper import FakeData
    from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op

    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"alice": auth.hash_password("correct horse battery")}))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(users))
    monkeypatch.setattr(alpaca_smoke.getpass, "getpass", lambda prompt="": "correct horse battery")
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    broker = ap.AlpacaPaperBroker(_PaperAccount(), FakeData(price=10.0),
                                  ledger=ap.OrderLedger(tmp_path / ap.LEDGER_NAME),
                                  sessions=auth.SessionStore(tmp_path / "auth.sqlite"))   # as --approve-order does
    report = alpaca_smoke.Report()
    account, clock, price, held = alpaca_smoke.read_checks(report, broker, "F", op.OrderLimits(1000.0))
    alpaca_smoke.order_checks(report, broker, "F", op.OrderLimits(1000.0), account, clock, price, held,
                              "alice", 0.5)
    assert broker.trading.submitted == []
    assert ("order.confirm", "FAIL") in [(n, s) for n, s, _ in report.rows]


@pytest.mark.unit
def test_smoke_scripts_never_hold_credentials():
    for name in ("live_gemini_langfuse", "live_alpaca_paper"):
        text = (ROOT / "scripts" / "smoke" / f"{name}.py").read_text()
        assert "sk-" not in text and "pk-lf" not in text and "AKIA" not in text
        import re
        assigned = re.findall(r'os\.environ\["([A-Z0-9_]+)"\]\s*=(?!=)', text)
        assert set(assigned) <= {"E3_RESULTS_DIR"}   # never sets a key; reads them from the environment


# ------------------------------------------------------------------ bounded calls and cost
def _scripted_service_run(tmp_path, monkeypatch, extra_callbacks, cancel):
    """A real service run (all four analysts) on scripted models that report usage."""
    from tests.test_e1_e2_e3_audit import Named
    from tests.test_graph_end_to_end import _Client
    from tradingagents import e1_routing, service
    from tradingagents.default_config import DEFAULT_CONFIG

    model = Named(model_name="gemini-3.1-flash-lite")
    monkeypatch.setattr(e1_routing, "create_tier_client", lambda c, t, **k: _Client(
        model.model_copy(update={"callbacks": k.get("callbacks")})))
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "r"), data_cache_dir=str(tmp_path / "c"),
                  memory_log_path=str(tmp_path / "m.md"))
    request = service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", config=config)
    return model, (lambda: service.run_analysis(request, cancel=cancel, extra_callbacks=extra_callbacks))


PRICING = {"gemini-3.1-flash-lite": {"input_per_million_usd": 0.25, "output_per_million_usd": 1.5}}


@pytest.mark.unit
def test_the_call_budget_stops_the_run(gemini_smoke, tmp_path, monkeypatch, offline):  # noqa: F811
    import threading

    from tradingagents import service

    cancel = threading.Event()
    guard = gemini_smoke.BudgetGuard(cancel, max_calls=5, max_cost=1.0, pricing=PRICING)
    model, run = _scripted_service_run(tmp_path, monkeypatch, [guard], cancel)
    with pytest.raises(service.AnalysisCancelled):
        run()
    assert len(model.calls) <= 5 and guard.calls == 5          # strict, even with parallel analysts (a full run makes 15)
    assert "max-calls" in guard.tripped


@pytest.mark.unit
def test_the_cost_budget_stops_the_run(gemini_smoke, tmp_path, monkeypatch, offline):  # noqa: F811
    import threading

    from tradingagents import service

    cancel = threading.Event()
    # 100 in / 20 out per call at flash-lite prices is $0.000055; stop after the second call.
    guard = gemini_smoke.BudgetGuard(cancel, max_calls=100, max_cost=0.0001, pricing=PRICING)
    model, run = _scripted_service_run(tmp_path, monkeypatch, [guard], cancel)
    with pytest.raises(service.AnalysisCancelled):
        run()
    assert "max-cost" in guard.tripped and guard.cost >= 0.0001
    assert len(model.calls) <= 4                               # the ceiling plus calls already in flight


@pytest.mark.unit
def test_an_unpriced_model_is_charged_at_the_highest_price(gemini_smoke):
    import threading
    from types import SimpleNamespace as NS

    from langchain_core.messages import AIMessage

    pricing = {"cheap": {"input_per_million_usd": 1, "output_per_million_usd": 1},
               "dear": {"input_per_million_usd": 10, "output_per_million_usd": 100}}
    guard = gemini_smoke.BudgetGuard(threading.Event(), 99, 99.0, pricing)
    guard.on_chat_model_start({}, [[]], run_id="a", metadata={"ls_model_name": "unknown"})
    message = AIMessage("x", usage_metadata={"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1})
    guard.on_llm_end(NS(llm_output=None, generations=[[NS(message=message)]]), run_id="a")
    assert guard.cost == 10.0


@pytest.mark.unit
def test_an_approved_run_is_bounded_before_it_starts(gemini_smoke, tmp_path, monkeypatch, capsys):
    """The approved path hands the service the budget and the round/retry caps (service stubbed)."""
    from tradingagents import service

    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps(PRICING))
    for name, value in {"GOOGLE_API_KEY": "g" * 30, "E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "p" * 20,
                        "LANGFUSE_SECRET_KEY": "s" * 20, "LANGFUSE_BASE_URL": "http://localhost.invalid",
                        "E3_PRICING_JSON": str(pricing)}.items():
        monkeypatch.setenv(name, value)
    defaults = dict(service.DEFAULT_CONFIG, llm_provider="google",
                    quick_think_llm="gemini-3.1-flash-lite", deep_think_llm="gemini-3.1-flash-lite")
    monkeypatch.setattr(service, "DEFAULT_CONFIG", defaults)
    seen = {}

    def stub(request, **kwargs):
        seen.update(request=request, **kwargs)
        raise RuntimeError("stub: no live call " + "g" * 30)

    monkeypatch.setattr(service, "run_analysis", stub)
    monkeypatch.setattr(gemini_smoke, "_langfuse_observations", lambda run_id: pytest.fail("not reached"))
    assert gemini_smoke.main(["--approve-live-llm", "--out", str(tmp_path / "out")]) == 1
    config = seen["request"].config
    assert (config["max_tool_rounds"], config["llm_max_retries"]) == (3, 2)
    assert (config["max_debate_rounds"], config["max_risk_discuss_rounds"]) == (1, 1)
    assert seen["request"].analysts == ("market",) and seen["cancel"] is not None
    assert type(seen["extra_callbacks"][0]).__name__ == "Guard"
    out = capsys.readouterr().out
    assert "g" * 30 not in out and "***" in out                 # the key in the error text is redacted


# ------------------------------------------------------------------ read-only Alpaca, secrets
@pytest.mark.unit
def test_read_only_mode_cannot_submit_or_cancel(alpaca_smoke, tmp_path, monkeypatch, capsys):
    from tests.test_alpaca_paper import FakeData
    from tradingagents.execution import alpaca_paper as ap

    account = _PaperAccount()
    writes = []
    for name in alpaca_smoke.WRITE_METHODS:
        monkeypatch.setattr(account, name, lambda *a, _n=name, **k: writes.append(_n), raising=False)
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    brokers = []

    def from_env(cls, ledger=None):
        brokers.append(cls(account, FakeData(price=10.0), ledger=ledger))
        return brokers[-1]

    monkeypatch.setattr(ap.AlpacaPaperBroker, "from_env", classmethod(from_env))
    monkeypatch.setenv("ALPACA_API_KEY", "k" * 20)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s" * 20)

    assert alpaca_smoke.main(["--approve-read", "--out", str(tmp_path / "out")]) == 0
    assert writes == [] and account.submitted == []
    broker = brokers[0]
    for name in alpaca_smoke.WRITE_METHODS:
        with pytest.raises(alpaca_smoke.ReadOnlyViolation):
            getattr(broker.trading, name)()
    with pytest.raises(alpaca_smoke.ReadOnlyViolation):
        broker.submit(object())
    assert writes == []
    assert "read.no_writes" in capsys.readouterr().out
    assert ap._base_url(broker.trading) == ap.PAPER_TRADING_URL      # reads still reach the real client


@pytest.mark.unit
def test_secrets_are_redacted_and_the_account_masked(monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT / "scripts" / "smoke"))
    from _safety import mask, redact

    monkeypatch.setenv("ALPACA_SECRET_KEY", "super-secret-value")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-not-a-real-key")
    assert redact("error: super-secret-value in AIza-not-a-real-key") == "error: *** in ***"
    assert mask("PA12345678") == "******5678"


@pytest.mark.unit
def test_alpaca_smoke_request_counts_are_bounded(alpaca_smoke, tmp_path, monkeypatch):
    """Counts every call the scripts make on the trading and data clients."""
    from tests.test_alpaca_paper import FakeData
    from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op

    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    counted = []

    class Counting:
        def __init__(self, inner):
            object.__setattr__(self, "_inner", inner)

        def __getattr__(self, name):
            value = getattr(self._inner, name)
            if callable(value) and not name.startswith("_"):
                def call(*a, **k):
                    counted.append(name)
                    return value(*a, **k)
                return call
            return value

    users = tmp_path / "users.json"
    users.write_text(json.dumps({"alice": auth.hash_password("correct horse battery")}))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(users))
    monkeypatch.setattr(alpaca_smoke.getpass, "getpass", lambda prompt="": "correct horse battery")
    monkeypatch.setattr("builtins.input", lambda prompt="": "buy 1 F at 5.00")
    account = _PaperAccount()
    broker = ap.AlpacaPaperBroker(Counting(account), Counting(FakeData(price=10.0)),
                                  ledger=ap.OrderLedger(tmp_path / ap.LEDGER_NAME),
                                  sessions=auth.SessionStore(tmp_path / "auth.sqlite"))
    limits, report = op.OrderLimits(1000.0), alpaca_smoke.Report()

    reads = alpaca_smoke.read_checks(report, broker, "F", limits)
    read_requests = list(counted)
    alpaca_smoke.order_checks(report, broker, "F", limits, *reads, "alice", 0.5)

    assert len(read_requests) <= 6, read_requests
    assert "submit_order" not in read_requests
    order_requests = counted[len(read_requests):]
    assert order_requests.count("submit_order") == 1 and order_requests.count("cancel_order_by_id") == 1
    assert len(order_requests) <= 40, order_requests        # 1 submit, 1 cancel, <= 30 status polls, checks
    assert order_requests.count("submit_order") == 1


# ------------------------------------------------------------------ --phase2-baseline
PHASE2_CONFIG = {"llm_provider": "google", "quick_think_llm": "gemini-3.5-flash-lite",
                 "deep_think_llm": "gemini-3.5-flash-lite", "quick_fallback_llm": "gemini-3.1-flash-lite",
                 "deep_fallback_llm": "gemini-3.1-flash-lite"}


def _ready_env(monkeypatch, tmp_path):
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}
                                   for m in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")}))
    for name, value in {"GOOGLE_API_KEY": "g" * 30, "E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "p" * 20,
                        "LANGFUSE_SECRET_KEY": "s" * 20, "LANGFUSE_BASE_URL": "http://localhost.invalid",
                        "E3_PRICING_JSON": str(pricing)}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.unit
def test_the_phase2_baseline_runs_with_the_phase2_settings(gemini_smoke, tmp_path, monkeypatch):
    from tradingagents import service
    from tradingagents.default_config import DEFAULT_CONFIG

    _ready_env(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "DEFAULT_CONFIG", dict(DEFAULT_CONFIG, **PHASE2_CONFIG))
    seen = {}

    def stub(request, **kwargs):
        seen.update(request=request, **kwargs)
        raise RuntimeError("stub: no live call")

    monkeypatch.setattr(service, "run_analysis", stub)
    assert gemini_smoke.main(["--approve-live-llm", "--phase2-baseline", "--out", str(tmp_path / "o")]) == 1
    config = seen["request"].config
    assert seen["request"].analysts == ("market",) and seen["request"].trade_date == "2026-10-23"
    assert (config["max_debate_rounds"], config["max_risk_discuss_rounds"], config["e2_cache_enabled"]) == (1, 1, False)
    assert config["max_tool_rounds"] == DEFAULT_CONFIG["max_tool_rounds"]         # no override, as in Phase 2
    assert config["llm_max_retries"] == DEFAULT_CONFIG["llm_max_retries"]
    assert {k: config[k] for k in PHASE2_CONFIG if k != "llm_provider"} == {
        k: v for k, v in PHASE2_CONFIG.items() if k != "llm_provider"}
    assert type(seen["extra_callbacks"][0]).__name__ == "Guard"                 # still bounded


@pytest.mark.unit
def test_the_phase2_baseline_refuses_other_models(gemini_smoke, tmp_path, monkeypatch, capsys):
    from tradingagents import service
    from tradingagents.default_config import DEFAULT_CONFIG

    _ready_env(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "DEFAULT_CONFIG", dict(DEFAULT_CONFIG, **dict(PHASE2_CONFIG, deep_think_llm="gemini-3.5-flash")))
    monkeypatch.setattr(service, "run_analysis", lambda *a, **k: pytest.fail("pre-flight must stop it"))
    assert gemini_smoke.main(["--approve-live-llm", "--phase2-baseline"]) == 1
    assert "the Phase 2 baseline used 'gemini-3.5-flash-lite'" in capsys.readouterr().out


@pytest.mark.unit
def test_the_comparison_reads_the_tracked_phase2_references(gemini_smoke):
    import hashlib

    refs = gemini_smoke.PHASE2_REFERENCES
    assert len(refs) == 2
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in refs]
    references = [json.loads(p.read_text()) for p in refs]

    like_phase2 = gemini_smoke.Report()
    gemini_smoke.compare_with_phase2(like_phase2, references[0], references)
    assert not like_phase2.failed and all(s == "PASS" for _, s, _ in like_phase2.rows)

    drifted = dict(references[0], per_call=[dict(c, input_tokens=c["input_tokens"] * 3) for c in references[0]["per_call"]])
    report = gemini_smoke.Report()
    gemini_smoke.compare_with_phase2(report, drifted, references)
    assert not report.failed and ("phase2.tokens_in", "WARN") in [(n, s) for n, s, _ in report.rows]

    runaway = dict(references[0], observed_llm_calls=30)
    report = gemini_smoke.Report()
    gemini_smoke.compare_with_phase2(report, runaway, references)
    assert ("phase2.calls", "FAIL") in [(n, s) for n, s, _ in report.rows]
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in refs] == before    # read-only


@pytest.mark.unit
def test_routing_check_with_one_model_on_both_tiers(gemini_smoke, tmp_path, monkeypatch, offline):  # noqa: F811
    """The Phase 2 baseline uses one model for both tiers: routing is not visible, so WARN, never a false FAIL;
    a model outside the configuration is still a FAIL."""
    from tests.test_e1_e2_e3_audit import _run

    result, _summary, _ = _run(tmp_path, monkeypatch, quick="gemini-3.5-flash-lite", deep="gemini-3.5-flash-lite")
    report = gemini_smoke.Report()
    gemini_smoke.check_run(report, result, "run1", max_cost=0.05, deep_model="gemini-3.5-flash-lite",
                           force_fallback=False, quick_model="gemini-3.5-flash-lite",
                           allowed_models=("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"))
    assert ("run1.e1.routing", "WARN") in [(n, s) for n, s, _ in report.rows] and not report.failed
    strict = gemini_smoke.Report()
    gemini_smoke.check_run(strict, result, "run1", max_cost=0.05, deep_model="gemini-3.5-flash-lite",
                           force_fallback=False, quick_model="gemini-3.5-flash-lite",
                           allowed_models=("gemini-3.1-flash-lite",))
    assert ("run1.e1.routing", "FAIL") in [(n, s) for n, s, _ in strict.rows]


# ------------------------------------------------------------------ provenance in every report
@pytest.mark.unit
def test_a_run_records_its_provenance_without_secrets(monkeypatch):
    import subprocess
    import sys

    sys.path.insert(0, str(ROOT / "scripts" / "smoke"))
    from _safety import provenance

    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-very-secret-value")
    info = provenance("live_gemini_langfuse.py", ["--approve-live-llm", "--max-cost", "0.04", "AIza-very-secret-value"])
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert (info["git_branch"], info["git_commit"]) == (branch, commit) and len(commit) == 40
    assert info["command"].endswith("scripts/smoke/live_gemini_langfuse.py --approve-live-llm --max-cost 0.04 '***'") or \
        info["command"].endswith("scripts/smoke/live_gemini_langfuse.py --approve-live-llm --max-cost 0.04 ***")
    assert "AIza-very-secret-value" not in json.dumps(info)
    assert info["started_at_utc"].endswith("+00:00") and isinstance(info["git_uncommitted_changes"], bool)


@pytest.mark.unit
def test_a_run_never_writes_into_an_earlier_runs_folder(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "scripts" / "smoke"))
    from _safety import fresh_dir

    first = tmp_path / "20261122_215240"
    assert fresh_dir(first) == first
    first.mkdir()
    assert fresh_dir(first) == tmp_path / "20261122_215240_2"
    (tmp_path / "20261122_215240_2").mkdir()
    assert fresh_dir(first) == tmp_path / "20261122_215240_3"


@pytest.mark.unit
def test_the_report_file_carries_the_run_provenance(alpaca_smoke, tmp_path, monkeypatch):
    from tests.test_alpaca_paper import FakeData
    from tradingagents.execution import alpaca_paper as ap

    account = _PaperAccount()
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    monkeypatch.setattr(ap.AlpacaPaperBroker, "from_env",
                        classmethod(lambda cls, ledger=None: cls(account, FakeData(price=10.0), ledger=ledger)))
    monkeypatch.setenv("ALPACA_API_KEY", "k" * 20)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret-secret-secret")
    out = tmp_path / "run"
    assert alpaca_smoke.main(["--approve-read", "--out", str(out)]) == 0
    report = json.loads((out / "smoke_report.json").read_text())
    assert set(report) == {"run", "checks"} and report["checks"]
    run = report["run"]
    assert run["git_branch"] and len(run["git_commit"]) == 40 and run["output_dir"] == str(out)
    assert "--approve-read" in run["command"] and "secret-secret-secret" not in json.dumps(report)



# ------------------------------------------------------------------ --approve-order: every outcome
@pytest.fixture
def order_run(alpaca_smoke, tmp_path, monkeypatch):
    """Signed in, typed back, on a fake paper account; returns (run, account, report_rows)."""
    from tests.test_alpaca_paper import FakeData
    from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op

    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    monkeypatch.setattr(alpaca_smoke.time, "sleep", lambda s: None)
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"alice": auth.hash_password("correct horse battery")}))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(users))
    monkeypatch.setattr(alpaca_smoke.getpass, "getpass", lambda prompt="": "correct horse battery")
    monkeypatch.setattr("builtins.input", lambda prompt="": "buy 1 F at 5.58")
    account = _PaperAccount()
    broker = ap.AlpacaPaperBroker(account, FakeData(price=11.17), ledger=ap.OrderLedger(tmp_path / ap.LEDGER_NAME),
                                  sessions=auth.SessionStore(tmp_path / "auth.sqlite"))
    limits = op.OrderLimits(1000.0)

    def run():
        report = alpaca_smoke.Report()
        reads = alpaca_smoke.read_checks(report, broker, "F", limits)
        alpaca_smoke.order_checks(report, broker, "F", limits, *reads, "alice", 0.5)
        return {n: (s, d) for n, s, d in report.rows}
    return run, account


def _cancels(account):
    return [o for o in account.orders.values() if str(o.status) == "canceled"]


@pytest.mark.unit
def test_one_order_one_cancel_all_checks_pass(order_run):
    run, account = order_run
    rows = run()
    assert all(s == "PASS" for s, _ in rows.values()), rows
    [sent] = account.submitted
    assert (sent.symbol, sent.qty, sent.side.value, sent.type.value, sent.limit_price) == ("F", 1.0, "buy", "limit", 5.58)
    assert sent.time_in_force.value == "day" and sent.extended_hours is False
    assert "LIMIT $5.58" in rows["order.parameters"][1] and "session-bound" in rows["order.parameters"][1]
    assert rows["order.final_status"] == ("PASS", "canceled")


@pytest.mark.unit
def test_a_failed_cancel_is_reported_with_the_final_status(order_run):
    run, account = order_run

    def refuse(order_id):
        raise RuntimeError("cancel refused")

    account.cancel_order_by_id = refuse
    rows = run()
    assert rows["order.cancel_request"][0] == "FAIL" and "cancel refused" in rows["order.cancel_request"][1]
    assert rows["order.final_status"][0] == "FAIL" and "not final after 30 s" in rows["order.final_status"][1]
    assert len(account.submitted) == 1                                   # no retry, no other order


@pytest.mark.unit
def test_an_unexpected_fill_needs_action_and_sends_nothing_else(order_run):
    run, account = order_run

    def fills(order_id):
        for o in account.orders.values():
            o.status, o.filled_qty, o.filled_avg_price = "filled", "1", "5.58"

    account.cancel_order_by_id = fills
    rows = run()
    assert rows["order.final_status"] == ("FAIL", "filled")
    assert rows["order.unfilled"][0] == "FAIL" and "ACTION REQUIRED" in rows["order.unfilled"][1]
    assert "Nothing is sold automatically" in rows["order.unfilled"][1]
    assert [o.side for o in account.submitted] == ["buy"]                  # no sell sent to "fix" it


@pytest.mark.unit
def test_an_ambiguous_submit_is_looked_up_once_and_cancelled_not_retried(order_run):
    run, account = order_run
    real = account.submit_order

    def accepted_then_timeout(order_data):
        real(order_data)
        raise TimeoutError("response lost")

    account.submit_order = accepted_then_timeout
    rows = run()
    assert rows["order.submitted"][0] == "FAIL" and "Alpaca holds order" in rows["order.submitted"][1]
    assert rows["order.final_status"] == ("PASS", "canceled") and len(_cancels(account)) == 1
    assert len(account.submitted) == 1


@pytest.mark.unit
def test_a_submit_that_really_failed_cancels_nothing(order_run):
    run, account = order_run

    def down(order_data):
        raise ConnectionError("unreachable")

    account.submit_order = down
    rows = run()
    assert rows["order.submitted"][0] == "FAIL" and "not placed" in rows["order.submitted"][1]
    assert "order.cancel_request" not in rows and account.orders == {}


@pytest.mark.unit
def test_an_interrupt_after_submit_still_cancels(order_run):
    run, account = order_run
    real_lookup = account.get_order_by_client_id
    calls = {"n": 0}

    def interrupted(client_id):
        calls["n"] += 1
        if account.submitted and calls["n"] >= 2:      # after the order exists, during the duplicate check
            raise KeyboardInterrupt
        return real_lookup(client_id)

    account.get_order_by_client_id = interrupted
    with pytest.raises(KeyboardInterrupt):
        run()
    assert len(account.submitted) == 1 and len(_cancels(account)) == 1


@pytest.mark.unit
def test_nothing_is_sent_without_the_typed_confirmation(order_run, monkeypatch):
    run, account = order_run
    monkeypatch.setattr("builtins.input", lambda prompt="": "buy 1 F at 5.59")   # one cent off
    rows = run()
    assert rows["order.confirm"][0] == "FAIL" and account.submitted == []


# ------------------------------------------------------------------ request meter: expected 404s vs errors
class _NotFound(Exception):
    status_code = 404


@pytest.mark.unit
@pytest.mark.parametrize("method, exc, outcome", [
    ("get_open_position", _NotFound("position does not exist"), "EXPECTED_NOT_FOUND"),
    ("get_order_by_client_id", Exception("order not found"), "EXPECTED_NOT_FOUND"),   # recognised by message too
    ("get_open_position", RuntimeError("500 internal server error"), "ERROR: RuntimeError"),
    ("get_order_by_client_id", ConnectionError("unreachable"), "ERROR: ConnectionError"),
    ("get_account", _NotFound("not found"), "ERROR: _NotFound"),          # a 404 elsewhere is not expected
    ("submit_order", _NotFound("not found"), "ERROR: _NotFound"),
    ("get_open_position", None, "ok"),
])
def test_request_outcomes_are_classified(alpaca_smoke, method, exc, outcome):
    assert alpaca_smoke.request_outcome(method, exc) == outcome


@pytest.mark.unit
def test_the_meter_returns_and_raises_exactly_as_the_client_does(alpaca_smoke):
    error = _NotFound("position does not exist")

    class Client:
        def get_open_position(self, symbol):
            raise error

        def get_account(self):
            return "account"

    log = []
    metered = alpaca_smoke.RequestMeter(Client(), log, "trading")
    assert metered.get_account() == "account"
    with pytest.raises(_NotFound) as caught:
        metered.get_open_position("F")
    assert caught.value is error                                           # the same exception, unchanged
    assert [r["outcome"] for r in log] == ["ok", "EXPECTED_NOT_FOUND"]
    assert set(log[0]) == {"client", "method", "outcome", "ms"}            # no arguments or results kept


def _main_with(alpaca_smoke, tmp_path, monkeypatch, account, argv):
    from tests.test_alpaca_paper import FakeData
    from tradingagents.execution import alpaca_paper as ap

    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    monkeypatch.setattr(ap.AlpacaPaperBroker, "from_env",
                        classmethod(lambda cls, ledger=None: cls(account, FakeData(price=11.17), ledger=ledger)))
    monkeypatch.setattr(alpaca_smoke.time, "sleep", lambda s: None)
    monkeypatch.setenv("ALPACA_API_KEY", "k" * 20)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s" * 20)
    out = tmp_path / "run"
    code = alpaca_smoke.main([*argv, "--out", str(out)])
    report = json.loads((out / "smoke_report.json").read_text())
    return code, {c["check"]: (c["status"], c["detail"]) for c in report["checks"]}


@pytest.mark.unit
def test_no_position_is_expected_not_found_and_passes(alpaca_smoke, tmp_path, monkeypatch):
    code, checks = _main_with(alpaca_smoke, tmp_path, monkeypatch, _PaperAccount(), ["--approve-read"])
    assert code == 0
    status, detail = checks["requests"]
    assert status == "PASS" and "trading.get_open_position" in detail
    assert "[EXPECTED_NOT_FOUND]" in detail and "0 unexpected errors" in detail and "failed" not in detail
    assert checks["read.positions"][0] == "PASS"                            # behaviour unchanged: 0 held


@pytest.mark.unit
def test_an_unexpected_error_is_still_an_error(alpaca_smoke, tmp_path, monkeypatch):
    account = _PaperAccount()

    def broken():
        raise RuntimeError("500 internal server error")

    account.get_all_positions = broken
    code, checks = _main_with(alpaca_smoke, tmp_path, monkeypatch, account, ["--approve-read"])
    assert code == 1
    status, detail = checks["requests"]
    assert status == "FAIL" and "[ERROR: RuntimeError]" in detail and "1 unexpected errors" in detail
    assert checks["unexpected"][0] == "FAIL" and account.submitted == []


@pytest.mark.unit
def test_the_order_paths_lookups_are_expected_not_found(alpaca_smoke, tmp_path, monkeypatch):
    from tradingagents.execution import identity as auth

    users = tmp_path / "users.json"
    users.write_text(json.dumps({"alice": auth.hash_password("correct horse battery")}))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(users))
    monkeypatch.setattr(alpaca_smoke.getpass, "getpass", lambda prompt="": "correct horse battery")
    monkeypatch.setattr("builtins.input", lambda prompt="": "buy 1 F at 5.58")
    account = _PaperAccount()
    code, checks = _main_with(alpaca_smoke, tmp_path, monkeypatch, account,
                              ["--approve-order", "--username", "alice"])
    assert code == 0, checks
    status, detail = checks["requests"]
    assert status == "PASS" and "trading.get_order_by_client_id" in detail
    # No position (read check + the broker's fresh pre-submit check), and no earlier order with this ID.
    # Durations vary with machine load, so match any; the labels and counts are what matter.
    import re

    assert len(re.findall(r"trading\.get_open_position [\d.]+ ms \[EXPECTED_NOT_FOUND\]", detail)) == 2
    assert len(re.findall(r"trading\.get_order_by_client_id [\d.]+ ms \[EXPECTED_NOT_FOUND\]", detail)) == 1
    assert detail.count("[EXPECTED_NOT_FOUND]") == 3 and "0 unexpected errors" in detail
    assert len(account.submitted) == 1 and checks["order.final_status"] == ("PASS", "canceled")
