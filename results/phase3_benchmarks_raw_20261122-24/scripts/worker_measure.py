"""Worker measurements, offline: cold/warm start, spare recovery, cancellation responsiveness."""
import copy, os, statistics, sys, tempfile, threading, time
sys.path.insert(0, os.getcwd())

def main():
    os.environ["TRADINGAGENTS_ANALYSIS_WORKER"] = "process"
    from tradingagents import service, worker
    from tradingagents.default_config import DEFAULT_CONFIG

    def request(tmp, analysts=("market", "news")):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.update(results_dir=f"{tmp}/r", data_cache_dir=f"{tmp}/c", memory_log_path=f"{tmp}/m.md")
        return service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", analysts=analysts, config=cfg)

    med = lambda xs: f"median {statistics.median(xs):.2f}s (min {min(xs):.2f}, max {max(xs):.2f}, n={len(xs)})"
    setup = "tests.worker_fakes:scripted"

    cold = []
    for _ in range(5):
        w = worker._Worker(setup); t = time.monotonic(); w.wait_ready(60); cold.append(time.monotonic() - t); w.stop()
    print("cold start to ready:          ", med(cold))

    pool = worker.pool_for(setup)
    warm, recovery = [], []
    for _ in range(5):
        pool.replenish()
        while pool._ready is None or not pool._ready.wait_ready(60): time.sleep(0.01)
        t = time.monotonic(); w = pool.take(); warm.append(time.monotonic() - t)
        t = time.monotonic(); w.conn.send(("run", request(tempfile.mkdtemp()), dict(os.environ)))
        while True:                                   # drain to the end of the run
            m = w.conn.recv()
            if m[0] in ("result", "error", "cancelled"): break
        w.process.join(5)
        t_end = time.monotonic(); pool.replenish()
        while pool._ready is None: time.sleep(0.01)
        pool._ready.wait_ready(60); recovery.append(time.monotonic() - t_end)
        pool.take().stop()
    print("warm spare hand-over:         ", med(warm))
    print("spare ready again after a run:", med(recovery))

    coop = []
    for _ in range(5):
        cancel = threading.Event(); t = {}
        def on_event(e, c=cancel, t=t):
            if e.kind == "step" and not c.is_set(): t["c"] = time.monotonic(); c.set()
        try: worker.run_in_worker(request(tempfile.mkdtemp()), on_event=on_event, cancel=cancel, grace=10, setup="tests.worker_fakes:slow_calls")
        except service.AnalysisCancelled as exc: coop.append(time.monotonic() - t["c"]); assert not exc.terminated
    print("cooperative cancel to release:", med(coop), "(model calls take 0.3 s each)")

    term = []
    for _ in range(3):
        tmp = tempfile.mkdtemp(); marker = f"{tmp}/marker"; os.environ["WORKER_FAKE_MARKER"] = marker
        cancel = threading.Event(); t = {}
        def watch(c=cancel, t=t, m=marker):
            while not os.path.exists(m): time.sleep(0.01)
            t["c"] = time.monotonic(); c.set()
        threading.Thread(target=watch, daemon=True).start()
        try: worker.run_in_worker(request(tmp), cancel=cancel, grace=2.0, setup="tests.worker_fakes:hung_market_call")
        except service.AnalysisCancelled as exc: term.append(time.monotonic() - t["c"]); assert exc.terminated
    print("hung call, grace 2 s, release:", med(term), f"| busy after: {service.is_busy()}")

if __name__ == "__main__":
    main()
