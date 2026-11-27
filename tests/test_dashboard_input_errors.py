"""F1: a bad ticker or no analyst is refused on the page with a message, and no run starts
(offline; AppTest drives the real page)."""

from pathlib import Path

import pytest

from tradingagents import service
from tradingagents.dashboard import jobs, trade
from tradingagents.execution import alpaca_paper as ap


@pytest.fixture
def page(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS):
        monkeypatch.delenv(name, raising=False)
    started = []
    monkeypatch.setattr(jobs, "start_job", lambda request: started.append(request) or pytest.fail("no run may start"))
    app = AppTest.from_file(str(Path(trade.__file__).with_name("app.py")), default_timeout=30).run()
    app.started = started
    return app


def _run(page, ticker="NVDA", analysts=("market",)):
    next(t for t in page.text_input if t.label == "Ticker").input(ticker)
    page.multiselect[0].set_value(list(analysts))
    next(b for b in page.button if b.label == "Run analysis").click()
    page.run()
    return [e.value for e in page.error]


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["NV DA", "../../etc", "$$$"])
def test_a_bad_ticker_is_refused_with_a_message(page, ticker):
    errors = _run(page, ticker=ticker)
    assert not page.exception and errors and "ticker" in errors[0].lower()
    assert page.started == [] and not service.is_busy()


@pytest.mark.unit
def test_no_analyst_is_refused_with_a_message(page):
    errors = _run(page, analysts=())
    assert not page.exception and page.started == []
    assert errors and "analyst" in errors[0].lower()
