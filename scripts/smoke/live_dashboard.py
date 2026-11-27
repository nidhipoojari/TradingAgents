#!/usr/bin/env python3
"""Live F1 test: ONE analysis through the real Streamlit dashboard in a headless browser.

Without ``--approve-live-llm`` it prints the plan and an offline pre-flight, makes no network call
and exits 2. With it, it starts ``tradingagents-dashboard`` (headless, paper trading off, per-run
limits on, fresh output folder), signs in a throwaway user, sets NVDA / 2026-10-23 / Market analyst /
Shallow, checks every pre-run setting, clicks Run exactly once and waits for a terminal state:

    completed | failed | cancelled | budget_exceeded

It stops as soon as one appears (a failure is captured with its message, a screenshot and the run
ID), never retries, and clicks Cancel only if the run is still running at ``--limit-seconds``.

Needs Playwright with a Chromium build (a test-only tool, not a project dependency); run this script
with a Python that has it, e.g. ``/path/to/playwright-venv/bin/python scripts/smoke/live_dashboard.py``.
The dashboard itself runs in the project's virtualenv (``--project-python``).

Exit code: 0 completed, 1 any other terminal state or a failed check, 2 not run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _safety import fresh_dir, provenance, redact  # noqa: E402

TICKER, DATE, ANALYST, DEPTH = "NVDA", "2026-10-23", "Market", "Shallow"
OTHER_ANALYSTS = ("Sentiment", "News", "Fundamentals")
MAX_CALLS, MAX_COST = 15, 0.04
# The validation run's analysis deadline: about twice the slowest healthy run measured offline (42.3 s: a
# 12-call workflow with two recovered primary 503s), so it never rejects a successful analysis, while a
# stuck one ends in half the 180 s production default.
VALIDATION_DEADLINE_SECONDS = 90
TERMINAL = ("completed", "failed", "cancelled", "budget_exceeded", "deadline_exceeded")


# ------------------------------------------------------------------ pure logic (tested offline)
def dashboard_env(out: Path, base: dict, *, max_calls=MAX_CALLS, max_cost=MAX_COST,
                  deadline_seconds=VALIDATION_DEADLINE_SECONDS, extra=None) -> dict:
    """The dashboard's environment for this test: headless, no Alpaca keys, limits on, all output in ``out``."""
    env = dict(base)
    env.update({
        # Streamlit asks for an e-mail address on its first interactive start and blocks on it; headless
        # mode skips that prompt (and opening a browser). Set here only, not in tradingagents-dashboard.
        "STREAMLIT_SERVER_HEADLESS": "true",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        # Paper trading off: with no keys the dashboard cannot reach Alpaca at all.
        "ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": "", "APCA_API_KEY_ID": "", "APCA_API_SECRET_KEY": "",
        "TRADINGAGENTS_E2_CACHE": "true",
        "TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS": str(max_calls),
        "TRADINGAGENTS_DASHBOARD_MAX_COST_USD": str(max_cost),
        "TRADINGAGENTS_ANALYSIS_DEADLINE_SECONDS": str(deadline_seconds),
        "TRADINGAGENTS_RESULTS_DIR": str(out / "results"),
        "TRADINGAGENTS_CACHE_DIR": str(out / "cache"),
        "TRADINGAGENTS_MEMORY_LOG_PATH": str(out / "memory" / "trading_memory.md"),
        "E3_RESULTS_DIR": str(out / "e3"),
        "TRADINGAGENTS_DASHBOARD_USERS": str(out / "users.json"),
        "TRADINGAGENTS_AUTH_DB": str(out / "auth.sqlite"),
    })
    env.update(extra or {})
    return env


def classify(view: dict) -> str | None:
    """The run's terminal state from what the page shows, or None while it is still running.

    ``view``: has_rating (the result's Rating metric), alerts (texts of error/warning boxes),
    cancel_button (the Cancel analysis button is shown), run_enabled (Run analysis is clickable).
    """
    alerts = " ".join(view.get("alerts") or [])
    if view.get("has_rating"):
        return "completed"
    if "Stopped by the per-run limit" in alerts:
        return "budget_exceeded"
    if "Stopped by the deadline" in alerts:
        return "deadline_exceeded"
    if "The analysis failed" in alerts:
        return "failed"
    if re.search(r"analysis \S+ was cancelled", alerts):
        return "cancelled"
    return None


def should_cancel(state: str | None, view: dict) -> bool:
    """Cancel only a run that is still running; a terminated run has nothing to cancel."""
    return state is None and bool(view.get("cancel_button"))


def preflight(env: dict, project_python: Path) -> list[str]:
    problems = []
    browser_problem = check_browser()
    if browser_problem:
        problems.append(browser_problem)
    if not project_python.is_file():
        problems.append(f"project Python not found: {project_python}")
    dotenv = ROOT / ".env"
    if not dotenv.is_file():
        problems.append("no .env in the repository root (Gemini and Langfuse keys are read from it)")
    pricing = env.get("E3_PRICING_JSON") or _dotenv_value(dotenv, "E3_PRICING_JSON")
    if not pricing:
        problems.append("E3_PRICING_JSON is not set: the dashboard refuses a cost limit without prices")
    return problems


def check_browser() -> str | None:
    """None when Playwright can launch headless Chromium here; otherwise what to do about it (offline)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("Playwright is not installed for this Python: run scripts/smoke/setup_playwright.sh "
                "(or pip install '.[e2e]'), then run this script with that Python")
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001 - turned into advice below
        text = str(exc)
        if "Executable doesn't exist" in text or "playwright install" in text:
            hint = "the browser is not installed: run scripts/smoke/setup_playwright.sh"
        elif "error while loading shared libraries" in text or "libnss3" in text or "libasound" in text:
            hint = ("Chromium's system libraries are missing: sudo apt-get install libnss3 libnspr4 libasound2t64, "
                    "or scripts/smoke/setup_playwright.sh --user-libs and set LD_LIBRARY_PATH as it prints")
        elif "not support" in text.lower():
            hint = "unsupported OS build: on Ubuntu 26.04 set PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64"
        else:
            hint = "see the error"
        return f"headless Chromium did not start ({hint}): {redact(text.splitlines()[0] if text else '')[:200]}"
    return None


def _dotenv_value(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{name}="):
            return line.split("=", 1)[1].strip() or None
    return None


# ------------------------------------------------------------------ the browser
def _view(page) -> dict:
    # Alerts in the main area only: the sidebar's notices (sign-in, paper trading) are not the run's outcome.
    alerts = page.locator('[data-testid="stAlert"]').evaluate_all(
        "els => els.filter(e => !e.closest('[data-testid=\"stSidebar\"]')).map(e => e.innerText)")
    return {"has_rating": page.get_by_text("Rating", exact=True).count() > 0,
            "alerts": alerts,
            "cancel_button": page.get_by_role("button", name="Cancel analysis").count() > 0,
            "run_enabled": page.get_by_role("button", name="Run analysis").is_enabled()}


def _settle(page, quiet=1.0, timeout=30):
    end = time.time() + timeout
    time.sleep(0.3)
    while time.time() < end:
        if page.locator('[data-testid="stStatusWidget"]').count() == 0:
            time.sleep(quiet)
            if page.locator('[data-testid="stStatusWidget"]').count() == 0:
                return
        time.sleep(0.2)


def _set_date(page, year, month, day):
    for part, value in (("year", year), ("month", month), ("day", day)):
        seg = page.get_by_role("spinbutton", name=f"{part}, Analysis date")
        seg.click()
        seg.press_sequentially(str(value).zfill(4 if part == "year" else 2), delay=40)
    page.keyboard.press("Tab")
    return tuple(page.get_by_role("spinbutton", name=f"{x}, Analysis date").get_attribute("aria-valuenow")
                 for x in ("year", "month", "day"))


def wait_for_terminal(page, timeout: float, poll: float = 1.0) -> tuple[str | None, dict]:
    end = time.time() + timeout
    view = {}
    while time.time() < end:
        view = _view(page)
        state = classify(view)
        if state is not None:
            return state, view
        time.sleep(poll)
    return None, view


def drive(url: str, password: str, shots: Path, limit_seconds: float) -> dict:
    from playwright.sync_api import sync_playwright

    out: dict = {"checks": {}, "console_errors": [], "page_errors": [], "failed_requests": [], "hosts": set()}

    def check(name, ok, detail=""):
        out["checks"][name] = {"ok": bool(ok), "detail": redact(str(detail))[:300]}
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {redact(str(detail))[:200]}", flush=True)

    with sync_playwright() as p:
        page = p.chromium.launch().new_page(viewport={"width": 1400, "height": 1000})
        page.on("console", lambda m: m.type == "error" and out["console_errors"].append(m.text[:200]))
        page.on("pageerror", lambda e: out["page_errors"].append(str(e)[:200]))
        page.on("requestfailed", lambda r: out["failed_requests"].append(r.url[:120]))
        page.on("request", lambda r: out["hosts"].add(re.sub(r"^\w+://([^/:]+).*", r"\1", r.url)))
        page.goto(url)
        page.get_by_text("TradingAgents", exact=True).first.wait_for(timeout=60_000)
        _settle(page)
        side = page.locator('[data-testid="stSidebar"]').inner_text()
        check("sidebar", "E2 data cache: on" in side and "E3 tracing: on" in side, re.sub(r"\s+", " ", side)[:250])
        check("paper_trading_off", "ALPACA_API_KEY" in side, "no Alpaca keys: paper trading switched off")
        # The limits panel, before the run: the deadline, the cost ceiling, and the call limit for one
        # analyst at Shallow depth (the first cell of the table's first row).
        ceiling = page.get_by_label("Estimated-cost ceiling for the next run ($)").input_value()
        # This script fixes the limit (TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS), so the panel states it;
        # without that it shows the derived table, whose first cell is one analyst at Shallow depth.
        calls = re.search(r"LLM-call limit:\s*(\d+)", side) or re.search(r"\b1\s+(\d+)\s+\d+\s+\d+", side)
        check("limits_shown", f"deadline {VALIDATION_DEADLINE_SECONDS:g} s" in side and float(ceiling) == MAX_COST
              and calls is not None and int(calls.group(1)) == MAX_CALLS,
              {"ceiling": ceiling, "market_only_shallow_calls": calls.group(1) if calls else None,
               "time": re.findall(r"Time:[^\n]*", side)[:1]})
        page.get_by_label("Username").fill("live-tester")
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in").click()
        _settle(page)
        check("signin", "Signed in as live-tester" in page.locator('[data-testid="stSidebar"]').inner_text())
        page.get_by_label("Ticker").fill(TICKER)
        date = _set_date(page, *map(int, DATE.split("-")))
        for other in OTHER_ANALYSTS:
            page.get_by_role("button", name=f"Remove {other}").click()
            _settle(page, 0.3)
        tags = [t.get_attribute("aria-label")
                for t in page.locator('[data-testid="stMultiSelect"] [data-tag]:visible').all()]
        depth = page.get_by_role("combobox", name="Research depth").input_value()
        check("form", tags == [ANALYST] and DEPTH in depth and date == tuple(str(int(x)) for x in DATE.split("-")),
              {"analysts": tags, "depth": depth, "date": date})
        page.screenshot(path=str(shots / "01_form.png"), full_page=True)
        if not all(c["ok"] for c in out["checks"].values()):
            out["state"] = "not_started"                 # a pre-run check failed: never click Run
            return out

        clicked = time.time()
        page.get_by_role("button", name="Run analysis").click()          # exactly once, never again
        state, view = wait_for_terminal(page, limit_seconds)
        if should_cancel(state, view):
            page.get_by_role("button", name="Cancel analysis").click()   # once, only while still running
            out["cancel_clicked_after_seconds"] = round(time.time() - clicked, 1)
            state, view = wait_for_terminal(page, 120)
        out["state"] = state or "no_terminal_state"
        out["seconds_to_terminal_state"] = round(time.time() - clicked, 1)
        _settle(page)
        body = page.locator('[data-testid="stApp"]').inner_text()
        out["message"] = redact(" | ".join(view.get("alerts") or []))[:1500]
        found = re.search(r"Run ID ([0-9]{8}-[0-9]{6}-[0-9a-f]{8})", body)
        out["run_id"] = found.group(1) if found else None
        page.screenshot(path=str(shots / f"02_{out['state']}.png"), full_page=True)
        if state == "completed":
            out["tabs"] = {}
            for tab in ("Decision", "Analysts", "Research debate", "Trader", "Risk debate", "Run metrics"):
                page.get_by_role("tab", name=tab).first.click()
                _settle(page, 0.5)
                out["tabs"][tab] = len(page.locator('[role="tabpanel"]:visible').first.inner_text())
                page.screenshot(path=str(shots / f"tab_{tab.replace(' ', '_')}.png"), full_page=True)
            check("tabs_rendered", all(n > 40 for n in out["tabs"].values()), out["tabs"])
            body = page.locator('[data-testid="stApp"]').inner_text()
        out["shown_metrics"] = [redact(line) for line in body.splitlines()
                                if re.search(r"Per-run limits:|E3 estimated cost:|Before it failed:|E1 backup"
                                             r"|E2 cache", line)][:12]
    out["hosts"] = sorted(h for h in out["hosts"] if h not in ("127.0.0.1", "localhost", ""))
    return out


# ------------------------------------------------------------------ the run
def _files(out: Path, run_id: str | None) -> dict:
    found = {}
    e3 = list((out / "e3").glob(f"*_{run_id}_e3.json")) if run_id else []
    if e3:
        found["e3"] = json.loads(e3[0].read_text(encoding="utf-8"))
    usage = out / "results" / TICKER / DATE / "token_usage.json"
    if usage.is_file():
        found["token_usage"] = json.loads(usage.read_text(encoding="utf-8"))
    log = out / "results" / "e1_fallback_log.txt"
    found["e1_fallback_log"] = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--approve-live-llm", action="store_true", help="run the ONE live analysis")
    parser.add_argument("--out", default=str(ROOT / "results" / "phase3_smoke" / f"dashboard_live_{time.strftime('%Y%m%d_%H%M%S')}"))
    parser.add_argument("--port", type=int, default=8599)
    parser.add_argument("--limit-seconds", type=float, default=300.0)
    parser.add_argument("--deadline-seconds", type=float, default=VALIDATION_DEADLINE_SECONDS,
                        help="the analysis deadline for this run (production default: 180)")
    parser.add_argument("--project-python", default=str(ROOT / ".venv" / "bin" / "python"))
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra dashboard environment (offline testing with scripted models)")
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw)
    extra = dict(item.split("=", 1) for item in args.env)
    project_python = Path(args.project_python)
    problems = preflight({**os.environ, **extra}, project_python)
    if not args.approve_live_llm:
        print(f"Live dashboard test -- NOT RUN (pass --approve-live-llm to run it)\n"
              f"  one analysis: {TICKER} {DATE}, {ANALYST} analyst, {DEPTH} depth; limits {MAX_CALLS} calls / "
              f"${MAX_COST}; Streamlit headless; Alpaca keys blanked; fresh folder under {Path(args.out).parent}")
        print("Pre-flight (offline):", "ready" if not problems else "NOT READY")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 2
    if problems:
        print("Pre-flight failed; nothing was run:")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 1

    out = fresh_dir(Path(args.out))
    (out / "browser").mkdir(parents=True)
    run_info = provenance(__file__, raw)
    print("Run:", json.dumps(run_info))
    password_file = out / "tester_password"
    created = subprocess.run(
        [str(project_python), "-I", "-c",
         "import secrets, sys; sys.path.insert(0, sys.argv[1]); from tradingagents.execution import identity as a;"
         " p = secrets.token_urlsafe(18); open(sys.argv[2], 'w').write(p); a.add_user(sys.argv[3], 'live-tester', p)",
         str(ROOT), str(password_file), str(out / "users.json")],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ""})
    if created.returncode != 0:
        print("could not create the test user:", redact(created.stderr)[-300:])
        return 1
    os.chmod(password_file, 0o600)
    env = dashboard_env(out, os.environ, deadline_seconds=args.deadline_seconds, extra=extra)
    launcher = project_python.with_name("tradingagents-dashboard")
    with (out / "server.log").open("w") as log:
        server = subprocess.Popen([str(launcher), "--server.port", str(args.port)], cwd=ROOT, env=env,
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{args.port}/"
    try:
        for _ in range(60):
            try:
                if urllib.request.urlopen(url + "_stcore/health", timeout=2).read() == b"ok":
                    break
            except OSError:
                pass
            if server.poll() is not None:
                print(f"the dashboard exited during start-up (code {server.returncode}); see server.log")
                return 1
            time.sleep(1)
        else:
            print("the dashboard did not become healthy within 60 s")
            return 1
        result = drive(url, password_file.read_text().strip(), out / "browser", args.limit_seconds)
    finally:
        server.terminate()
        try:
            server.wait(30)
        except subprocess.TimeoutExpired:
            server.kill()
    result.update(run=dict(run_info, finished_at_local=time.strftime("%Y-%m-%dT%H:%M:%S%z"), output_dir=str(out)),
                  files=_files(out, result.get("run_id")))
    (out / "live_dashboard_report.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: result.get(k) for k in ("state", "run_id", "seconds_to_terminal_state", "message",
                                                 "shown_metrics", "console_errors", "page_errors", "hosts")},
                     indent=1, default=str))
    return 0 if result.get("state") == "completed" and all(c["ok"] for c in result["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
