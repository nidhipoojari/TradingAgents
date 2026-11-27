"""Scratch mutant: the parent's step clock starts when the run is requested, not when the call starts."""
import time

def pytest_configure(config):
    from tradingagents import worker
    orig_start = worker._Tally.start
    def start(self, run_id, agent, model, at, step=None):
        created = self.__dict__.setdefault("_created", None)
        orig_start(self, run_id, agent, model, at, step)
        if step is not None:
            self.step_started[step] = min(self.step_started[step], getattr(worker, "_MUTANT_T0", at))
    worker._Tally.start = start
    orig_run = worker.run_in_worker
    def run(*a, **k):
        worker._MUTANT_T0 = time.time()
        return orig_run(*a, **k)
    worker.run_in_worker = run
