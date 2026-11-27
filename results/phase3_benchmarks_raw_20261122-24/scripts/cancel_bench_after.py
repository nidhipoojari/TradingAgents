import copy, os, sys, tempfile, threading, time
sys.path.insert(0, os.getcwd())
os.environ["TRADINGAGENTS_ANALYSIS_WORKER"] = "process"
from tradingagents import service, worker
from tradingagents.default_config import DEFAULT_CONFIG

def request(tmp):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=f"{tmp}/r", data_cache_dir=f"{tmp}/c", memory_log_path=f"{tmp}/m.md")
    return service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", analysts=("market", "news"), config=cfg)


import faulthandler
def main():
    faulthandler.dump_traceback_later(60, exit=True)
    for grace in (2.0, 10.0):
        tmp = tempfile.mkdtemp(); marker = f"{tmp}/marker"; os.environ["WORKER_FAKE_MARKER"] = marker
        cancel, t = threading.Event(), {}
        def watch():
            while not os.path.exists(marker): time.sleep(0.01)
            t["cancel"] = time.monotonic(); cancel.set()
        threading.Thread(target=watch, daemon=True).start()
        try: worker.run_in_worker(request(tmp), cancel=cancel, grace=grace, setup="tests.worker_fakes:hung_market_call")
        except service.AnalysisCancelled as exc: t["released"] = time.monotonic(); term = exc.terminated
        print(f"AFTER worker, call never returns, grace {grace:>4}s: released {t['released'] - t['cancel']:.2f}s after cancel, terminated={term}, busy={service.is_busy()}")

    # start-up: cold worker vs warm worker (time from request to first event)
    for label in ("cold", "warm"):
        tmp = tempfile.mkdtemp(); first = {}
        began = time.monotonic()
        worker.run_in_worker(request(tmp), on_event=lambda e: first.setdefault("t", time.monotonic()), setup="tests.worker_fakes:scripted")
        print(f"{label} worker: first event after {first['t'] - began:.2f}s, whole run {time.monotonic() - began:.2f}s")
        if label == "cold":
            time.sleep(12)        # let the pool's replacement worker finish importing


if __name__ == "__main__":
    main()
