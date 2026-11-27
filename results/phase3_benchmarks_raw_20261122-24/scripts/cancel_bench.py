"""Before/after: time from cancel to slot release, with a model call stuck in flight."""
import copy, os, sys, tempfile, threading, time
sys.path.insert(0, os.getcwd())
os.environ["TRADINGAGENTS_ANALYSIS_WORKER"] = "process"
from tradingagents import service, worker
from tradingagents.default_config import DEFAULT_CONFIG
import tests.worker_fakes as fakes

def request(tmp):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=f"{tmp}/r", data_cache_dir=f"{tmp}/c", memory_log_path=f"{tmp}/m.md")
    return service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", analysts=("market", "news"), config=cfg)

def measure(run, marker, limit=40.0):
    cancel, t = threading.Event(), {}
    def watch():
        while not os.path.exists(marker): time.sleep(0.01)
        t["cancel"] = time.monotonic(); cancel.set()
    threading.Thread(target=watch, daemon=True).start()
    def go():
        try: run(cancel)
        except service.AnalysisCancelled: t["released"] = time.monotonic()
    th = threading.Thread(target=go, daemon=True); th.start(); th.join(limit)
    return round(t["released"] - t["cancel"], 2) if "released" in t else f"not released after {limit - 0:.0f}s"

# BEFORE: in-process service, the stuck call returns 8 s after it starts (a slow provider)
tmp = tempfile.mkdtemp(); marker = f"{tmp}/marker"; os.environ["WORKER_FAKE_MARKER"] = marker
import tradingagents.e1_routing  # noqa
fakes._offline_vendors()
model = fakes._scripted()
class Slow(type(model)):
    def _generate(self, messages, stop=None, run_manager=None, **kw):
        if "get_stock_data" in {t.name for t in self.tools} and not os.path.exists(marker):
            open(marker, "w").close(); time.sleep(8)
        return super()._generate(messages, stop, run_manager, **kw)
fakes._models(Slow())
print("BEFORE in-process, call returns 8 s later :", measure(lambda c: service.run_analysis(request(tmp), cancel=c), marker), "s")
tmp2 = tempfile.mkdtemp(); marker2 = f"{tmp2}/marker"; os.environ["WORKER_FAKE_MARKER"] = marker2
fakes._models(fakes._scripted(block_market=True))
print("BEFORE in-process, call never returns     :", measure(lambda c: service.run_analysis(request(tmp2), cancel=c), marker2, 30.0))
