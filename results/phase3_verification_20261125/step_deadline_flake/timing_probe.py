"""Scratch pytest plugin: time the phases of worker.run_in_worker without changing the code under test."""
import functools, json, os, time
LOG = os.environ["PROBE_LOG"]
current = {"test": None}

def _log(**kw):
    with open(LOG, "a") as f:
        f.write(json.dumps({"test": current["test"], "t": round(time.monotonic(), 3), **kw}) + "\n")

def pytest_runtest_setup(item):
    current["test"] = item.nodeid.split("::")[-1]

def pytest_configure(config):
    from tradingagents import worker
    def wrap(owner, name):
        orig = getattr(owner, name)
        @functools.wraps(orig)
        def inner(*a, **k):
            t0 = time.monotonic()
            try:
                return orig(*a, **k)
            finally:
                _log(fn=name, seconds=round(time.monotonic() - t0, 3))
        setattr(owner, name, inner)
    wrap(worker.WorkerPool, "take")
    wrap(worker._Worker, "stop")
    wrap(worker, "_write_terminated_e3")
    wrap(worker, "_write_partial_usage")
    wrap(worker, "run_in_worker")
    orig_overdue = worker._Tally.overdue_step
    def overdue(self, now, limit):
        r = orig_overdue(self, now, limit)
        if r: _log(fn="overdue_detected", agent=r[0], waited=round(r[1], 3))
        return r
    worker._Tally.overdue_step = overdue

def pytest_runtest_call(item):
    import multiprocessing as mp
    from tradingagents import worker
    spares = {w.process.pid for p in worker._POOLS.values() if (w := p._ready)}
    kids = [(c.pid, c.pid in spares) for c in mp.active_children() if c.name == "tradingagents-analysis" and c.is_alive()]
    _log(fn="children_at_start", children=kids)

def _wrap_reap():
    from tradingagents import worker
    orig = worker._reap
    def reap(w):
        t0 = time.monotonic()
        try:
            return orig(w)
        finally:
            _log(fn="_reap", seconds=round(time.monotonic() - t0, 3), exitcode=w.process.exitcode)
    worker._reap = reap
_orig_configure = pytest_configure
def pytest_configure(config):
    _orig_configure(config)
    _wrap_reap()
