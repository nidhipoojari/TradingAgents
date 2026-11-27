<p align="center">
  <img src="tradingagents/assets/tauric-logo.svg" width="60%" alt="Tauric Research">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>
<br>
<div align="center">
  <a href="https://github.com/TauricResearch" target="_blank"><img alt="TradingAgents #1 Repository of the Day" src="https://trendshift.io/api/badge/repositories/16192" width="250" height="55"/></a>
</div>
<br>
<div align="center">
  <!-- Keep these links. Translations will automatically update with the README. -->
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=zh">中文</a>
</div>

---

# TradingAgents: Multi-Agents LLM Financial Trading Framework

## TradeDesk AI: SENG 691 term project fork

This repo is a SENG 691 (AI Agent Computing) term project built on
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).
Team: Joe Defusco, Shubhan Bhat, Nidhi Poojari.

| Phase | What it adds | Code |
| --- | --- | --- |
| Phase 1 | Original project built, deployed and run | commit `92d7dd0` |
| Phase 2 | E1 model routing + backup model, E2 market-data cache, E3 tracing + cost monitoring | commit `aa763f7` (PRs #1–#12) |
| Phase 3 | F1 Streamlit dashboard + human-approved Alpaca paper trading, F2 MCP server, one shared analysis service, Makefile | `main` (PRs #14–#15) |

File-by-file change lists: [PHASE2_CHANGES.md](PHASE2_CHANGES.md) and [PHASE3_CHANGES.md](PHASE3_CHANGES.md).

### Quick start

You need Python 3.11 or newer, git, and a free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

```bash
git clone https://github.com/nidhipoojari/TradingAgents.git
cd TradingAgents
make install      # creates .venv, installs everything, creates .env from .env.example
```

Open `.env` and set `GOOGLE_API_KEY`. Then:

| Command | What it does |
| --- | --- |
| `make doctor` | Checks Python, packages and `.env` (never prints key values, makes no model calls) |
| `make cli` | Runs an analysis in the terminal |
| `make dashboard` | Opens the F1 dashboard at http://127.0.0.1:8501 |
| `make mcp` | Starts the F2 MCP server (stdio) |
| `make test` | Runs the offline tests (no API keys or network needed) |
| `make lint` | Runs Ruff |
| `make analyze` | Writes cloc + lizard reports for the Phase 3 code to `eval/phase3/` |
| `make docker` | Runs the CLI in Docker |
| `make clean` | Deletes caches and build files |

**Windows:** there's no `make`, so use `build.ps1` with the same names, for example `.\build.ps1 install` or `.\build.ps1 dashboard`.

**Without make:** `python3 -m venv .venv`, activate it, then `python -m pip install -e ".[dashboard,mcp,e3]"` and `cp .env.example .env`.

### Phase 3 features

- **Shared analysis service** (`tradingagents/service.py`, `tradingagents/worker.py`): the CLI, the dashboard and the MCP server all run analyses through the same code, so E1, E2 and E3 work the same everywhere. Dashboard and MCP runs happen in a separate worker process with a time limit, a cap on model calls and a cost budget. Only one analysis runs at a time; a second request gets a "busy" message.
- **F1, dashboard + paper trading** (`tradingagents/dashboard/`, `tradingagents/execution/`): run an analysis, follow its progress, read the reports and debates, and see the model, token, cost and cache numbers. The final rating becomes a trade proposal, and only a signed-in person can approve it.
- **F2, MCP server** (`tradingagents/mcp_server.py`, `tradingagents/mcp_read_tools.py`): lets AI assistants such as Claude Code use TradeDesk AI through five read-only tools. No tool can approve or place an order.

### Using the dashboard (F1)

1. Run `make dashboard` and open http://127.0.0.1:8501 (it only listens on this computer).
2. Pick a ticker, a date and the analysts, then start the run.
3. When it finishes, read the reports, the debate and the run metrics.
4. To trade, check the proposal, enter the number of shares, approve it and type your password again. Or reject it. Both choices are logged.

How a rating becomes a proposal:

| Rating | Proposal |
| --- | --- |
| Buy, Overweight | Buy (you choose the quantity) |
| Sell, Underweight | Sell, only if the paper account holds the stock (no short selling) |
| Hold | No order, with the reason shown |

Paper trading needs Alpaca **paper** keys and a dashboard user (see the settings below). Without Alpaca keys, everything else in the dashboard still works.

Safety rules:

- Orders only go to Alpaca's paper endpoint (`paper=True`). A live URL or `ALPACA_PAPER=false` is refused.
- Each order is capped at $1,000 by default (`TRADINGAGENTS_MAX_ORDER_NOTIONAL`).
- Every order needs a signed-in user and the password again at approval time.
- Approvals and rejections go to `approval_decisions.jsonl`, and every order attempt goes to `paper_orders.jsonl`. An order is logged before it is sent.

Create a dashboard user:

```bash
python -m tradingagents.execution.identity add-user YOUR_NAME --file ~/.tradingagents/dashboard_users.json
# then in .env: TRADINGAGENTS_DASHBOARD_USERS=/full/path/to/dashboard_users.json
```

### Using the MCP server (F2)

| Tool | What it does |
| --- | --- |
| `analyze_stock` | Runs a new analysis (uses Gemini; limited by the MCP call and cost limits) |
| `get_report` | Returns a saved report, or one section of it |
| `get_run_metrics` | Returns time, tokens, cost, E1 switches and E2 cache counts for a run |
| `list_recent_runs` | Lists recent runs (up to 50) |
| `propose_trade` | Returns the trade proposal for a run, as information only |

Connect Claude Code (run this from the project folder, so the server finds `.env`):

```bash
claude mcp add tradingagents -- "$PWD/.venv/bin/tradingagents-mcp"
```

Then type `/mcp` in Claude Code to check that `tradingagents` shows five tools. stdio is the default and recommended transport. HTTP is also available (`tradingagents-mcp --transport http`), but only on 127.0.0.1 and only with `TRADINGAGENTS_MCP_TOKEN` set.

### Phase 3 settings

All are optional and go in `.env`. Leaving them out keeps the defaults. `.env.example` lists every setting with a comment.

| Setting | Feature | Default / example |
| --- | --- | --- |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | F1 paper trading | from your Alpaca paper account |
| `TRADINGAGENTS_MAX_ORDER_NOTIONAL` | F1 largest order in dollars | `1000` |
| `TRADINGAGENTS_DASHBOARD_USERS` | F1 users file (password sign-in) | `/path/to/dashboard_users.json` |
| `TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS`, `TRADINGAGENTS_DASHBOARD_MAX_COST_USD` | F1 per-run limits | `15`, `0.04` |
| `TRADINGAGENTS_MCP_MAX_LLM_CALLS`, `TRADINGAGENTS_MCP_MAX_COST_USD` | F2 per-run limits | 1.5 × the expected calls, `0.04` |
| `TRADINGAGENTS_MCP_TIMEOUT_SECONDS` | F2 time limit for `analyze_stock` | `900` |
| `TRADINGAGENTS_MCP_TOKEN` | F2 token, needed only for HTTP | 16+ characters |
| `E3_PRICING_JSON` | Prices for the cost limits (the dashboard and MCP need it) | `e3_pricing.json` |

### Phase 3 results

Phase 2 code vs Phase 3 code under the same conditions (NVDA, 2026-10-23, market analyst only, E2 on):

| Metric | Phase 2 (3 runs) | Phase 3 (1 run) |
| --- | --- | --- |
| Run time | 26.8–30.2 s (median 29.6 s) | 27.4 s |
| LLM calls | 10–11 | 10 |
| Tokens | 31,341–32,793 | 32,393 |
| Estimated cost | $0.0204–$0.0217 | $0.0213 |

The new features didn't make the agent slower or more expensive.

| Other Phase 3 checks | Result |
| --- | --- |
| Live dashboard run, NVDA, all four analysts | 31.1 s click to result, 16 calls, 59,952 tokens, about $0.034 |
| Gemini 429 during that run | E1 opened the circuit breaker and the backup model finished the run |
| Offline tests | 1,391 in Phase 2 → 2,114 in Phase 3 (2,113 pass, 1 skipped) |
| Same workload through CLI, dashboard and MCP | Same rating, report, calls and tokens |
| Risk-debate prompt fix | Each argument is now sent once: about 1,285 fewer input tokens per four-analyst run (estimated from saved prompts) |

Evidence files: `results/phase3_matched_nvda_20261127/`, `results/phase3_final_eval_20261127/` (its README lists every folder), and [PHASE3_HANDOFF.md](PHASE3_HANDOFF.md).

### Phase 2 enhancements

- **E1, model routing + backup model** (`tradingagents/e1_routing.py`): one table gives each of the 12 agents a quick or deep model. If a model call fails (rate limit, server error, unknown model), the same request goes to a backup model, so the run finishes instead of crashing. Each switch is logged to `<results_dir>/e1_fallback_log.txt`. Phase 3 added bounded retries and a circuit breaker.
- **E2, market-data cache** (`tradingagents/e2_cache.py`): past-dated answers from every data tool are saved in a local SQLite file, so repeat requests skip the download. Today's data and error answers are never cached.
- **E3, tracing + cost monitoring** (`tradingagents/e3_tracing.py`, `tradingagents/e3_cli_hook.py`): every LLM call is traced in Langfuse with its agent, model, time, tokens and errors, and a JSON summary with an estimated cost (`e3_pricing.json`) is saved after each run.

Phase 2 settings in `.env` (all optional; leaving them out keeps the original behavior):

| Setting | Enhancement | Example |
| --- | --- | --- |
| `TRADINGAGENTS_QUICK_FALLBACK_LLM`, `TRADINGAGENTS_DEEP_FALLBACK_LLM` | E1 backup models | `gemini-3.1-flash-lite` |
| `TRADINGAGENTS_E2_CACHE` | E2 cache on/off | `true` |
| `E3_TRACE_ENABLED` | E3 tracing on/off | `1` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | E3 Langfuse keys | from your Langfuse project |
| `E3_PRICING_JSON`, `E3_RESULTS_DIR` | E3 price file and summary folder | `e3_pricing.json`, `e3_results` |

Run one analysis without prompts:

```bash
python -m cli.main --ticker NVDA --date 2026-10-23 --analysts market --no-checkpoint --save --no-show
```

### Phase 2 results

Phase 1 code vs Phase 2 code, run under the same conditions (NVDA, 2026-10-23, market analyst, 3 runs each):

| Metric | Phase 1 | Phase 2 |
| --- | --- | --- |
| Run time, median | 46.4 s | 29.6 s |
| Tokens per run, mean | 135,240 | 31,933 |
| Estimated cost per run, mean | $0.0533 | $0.0210 |
| Same rating in 3 runs | 2 of 3 | 3 of 3 |
| Run with a broken main model (E1) | stops | finishes on the backup |
| Data-load time, warm cache (E2) | 3.69 s | 0.003 s |

Evidence files: `results/phase2/`, `eval/` (forced-failure test, Phase 1 vs Phase 2 runs, cloc and lizard reports) and the scripts in `scripts/`.

### Tests

`make test` (or `python -m pytest -q`) runs the offline suite. It needs no API keys or network. Install the extras first (`make install`), or the dashboard and Alpaca tests fail with `No module named 'streamlit'`.

On macOS, 3 tests in `tests/test_cache_writes.py` fail. They come from the original TradingAgents code and simulate Windows file locking, which doesn't work on macOS.

## News

<!-- news:start -->
- [2026-10] **TradingAgents v0.7.0** released with runs on your own ChatGPT Plus or Pro plan, backtests scored from the first close after each decision, a note at start-up when a newer release is out, and a wheel attached to each GitHub release.
- [2026-10] **TradingAgents v0.6.0** released with reports saved as one HTML page, a provider per model tier so the managers and analysts can run on different models, past decisions settled for every ticker while the analysts work, and company news read from Yahoo search while Yahoo's news feed is down.
- [2026-09] **TradingAgents v0.5.2** released with parallel analysts for a faster analysis, a CLI that runs without prompts from flags such as `--ticker` and `--date`, the run's settings recorded in every report, and backtests that see only data published by each analysis date.

Full release notes are in [CHANGELOG.md](CHANGELOG.md).

<details>
<summary>Earlier news</summary>

- [2026-09] **TradingAgents v0.5.1** released with a package layout organised by what each module holds (import paths moved), optional Jev screening of social posts, GPT-6 Sol and Luna as the default models, and fixes to run isolation and SEC EDGAR statements.
- [2026-09] **TradingAgents v0.5.0** released with point-in-time integrity across every dated path, SEC EDGAR fundamentals served as filed, backtesting over a ticker and date grid, portfolio-aware runs, and current model lineups across every provider.
- [2026-08] **TradingAgents v0.4.0** released with look-ahead / point-in-time fixes across FRED macro, social sentiment, and the decision-log memory; clearer decision signals; working CLI checkpoint resume; Trader price grounding; and the GPT-5.6 and GLM-5.3 models.
- [2026-07] **TradingAgents v0.3.1** released with correctness and stability fixes: Alpha Vantage look-ahead filtering, graph-router crash-safety, graph-shape-aware checkpoint resume, working crypto sentiment sources, a configurable LLM retry budget, Bedrock API-key auth, and Claude Sonnet 5 / Fable 5 support.
- [2026-06] **TradingAgents v0.3.0** released with a verified data-access contract, an expanded provider registry (NVIDIA, Kimi, Groq, Mistral, Bedrock, and any OpenAI-compatible endpoint), FRED and Polymarket data vendors, a current-generation model catalog, and a CI gate.
- [2026-05] **TradingAgents v0.2.5** released with the grounded Sentiment Analyst, GPT-5.5 etc. model coverage, Qwen/GLM/MiniMax dual-region support, `TRADINGAGENTS_*` env-var configurability with API-key auto-detection, remote Ollama support, non-US alpha benchmarks, and ticker path-traversal hardening.
- [2026-04] **TradingAgents v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix.
- [2026-03] **TradingAgents v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- [2026-03] **TradingAgents v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- [2026-02] **TradingAgents v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- [2026-01] **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released, with [Terminal](https://github.com/TauricResearch/Trading-R1) expected to land soon.

</details>
<!-- news:end -->

<div align="center">

🚀 [TradingAgents](#tradingagents-framework) | ⚡ [Installation & CLI](#installation-and-cli) | 🎬 [Demo](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [Package Usage](#tradingagents-package) | 🤝 [Contributing](#contributing) | 📄 [Citation](#citation)

</div>

> 🎉 **TradingAgents** officially released! We have received numerous inquiries about the work, and we would like to express our thanks for the enthusiasm in our community.
>
> So we decided to fully open-source the framework. Looking forward to building impactful projects with you!

## TradingAgents Framework

TradingAgents is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents: from fundamental analysts, sentiment experts, and technical analysts, to trader, risk management team, the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

Our framework decomposes complex trading tasks into specialized roles.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Aggregates news headlines, StockTwits, and Reddit chatter into a single sentiment read to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

The selected analysts work at the same time, each on its own tools, and the research debate starts once all of their reports are in.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions, determining the timing and magnitude of trades.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

Clone TradingAgents:
```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

TradingAgents needs Python 3.11 or later. Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n tradingagents python=3.13
conda activate tradingagents
```

Or with [uv](https://docs.astral.sh/uv/):
```bash
uv venv --python 3.13
source .venv/bin/activate
```

Install the package and its dependencies (`uv pip install .` with uv):
```bash
pip install .
```

### Docker

Alternatively, run with Docker:
```bash
cp .env.example .env  # add your API keys
docker compose run --rm tradingagents
```

After updating the repository, rebuild the image with `docker compose build`.

Results, reports, the memory log and the cache live in the `tradingagents_data` volume. To keep them in a folder on the host instead, create the folder and point `TRADINGAGENTS_DATA_DIR` at it, in `.env` or the shell: `mkdir -p data && TRADINGAGENTS_DATA_DIR=./data docker compose run --rm tradingagents`.

For local models with Ollama:
```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

### Required APIs

TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen (international, dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen (China, dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax (global, api.minimax.io)
export MINIMAX_CN_API_KEY=...      # MiniMax (China, api.minimaxi.com)
export OPENROUTER_API_KEY=...      # OpenRouter
export MISTRAL_API_KEY=...         # Mistral
export MOONSHOT_API_KEY=...        # Kimi (Moonshot)
export GROQ_API_KEY=...            # Groq
export NVIDIA_API_KEY=...          # NVIDIA NIM
export FRED_API_KEY=...            # FRED macro data (free, optional)
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
export TYPESAFE_API_KEY=...        # Jev social-post screening (optional)
```

For Azure OpenAI, copy `.env.enterprise.example` to `.env.enterprise` and fill in your credentials.

To run on your ChatGPT Plus or Pro plan instead of an API key, choose "Subscription" in the CLI's provider menu, or set `llm_provider: "chatgpt"`. Sign in once with `tradingagents login chatgpt`: it prints an address to open in your browser, which returns to this machine on `127.0.0.1`. When the browser runs on another machine, or TradingAgents runs in Docker (`docker compose run --rm tradingagents login chatgpt`), paste the address the browser ends up on. The sign-in is kept in `~/.tradingagents/chatgpt-auth.json`, and `tradingagents logout chatgpt` revokes it. Runs count toward your plan's usage limits, and the plan does not accept `temperature` or `max_tokens`.

For AWS Bedrock, install the extra with `pip install ".[bedrock]"`, set `llm_provider: "bedrock"`, configure AWS credentials (environment variables, `~/.aws/credentials`, or an IAM role) and `AWS_DEFAULT_REGION`, and use a Bedrock model ID, e.g. `us.anthropic.claude-opus-5-5`.

For local models, configure Ollama with `llm_provider: "ollama"`. The default endpoint is `http://localhost:11434/v1`; set `OLLAMA_BASE_URL` to point at a remote `ollama-serve`. Pull models with `ollama pull <name>`, and pick "Custom model ID" in the CLI for any model not listed by default.

For any other OpenAI-compatible server (vLLM, LM Studio, llama.cpp, or a custom relay), use `llm_provider: "openai_compatible"` and set the endpoint via `backend_url` (or `TRADINGAGENTS_LLM_BACKEND_URL`), e.g. `http://localhost:8000/v1` for vLLM or `http://localhost:1234/v1` for LM Studio. The model is whatever your server serves. No key is needed for local servers; set `OPENAI_COMPATIBLE_API_KEY` when the endpoint requires one.

With `TYPESAFE_API_KEY` set, the Sentiment Analyst screens StockTwits and Reddit posts with TypeSafe's Jev before reading them. Posts that are not about the company are dropped, and each source opens with a count of the remaining posts by stance: bullish, bearish, neutral, or unclear. Without the key, posts pass through unscreened. `jev-latest` moves with new releases; set `TYPESAFE_DEFAULT_MODEL` to a versioned ID such as `jev-1.13.0` to hold it fixed across runs. To reach Jev through OpenRouter, put an OpenRouter key in `TYPESAFE_API_KEY` and set `TYPESAFE_BASE_URL=https://openrouter.ai/api`.

Alternatively, copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

### CLI Usage

Launch the interactive CLI:
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```
You will see a screen where you can select your desired tickers, analysis date, LLM provider, research depth, and more. Your previous run's answers come back as the defaults, so pressing Enter accepts them. The `TRADINGAGENTS_*` variables in `.env` still skip their step entirely.

To run without questions, for a scheduled job or a script, answer the per-run steps with flags and the rest with `TRADINGAGENTS_*` variables:
```bash
export TRADINGAGENTS_LLM_PROVIDER=openai TRADINGAGENTS_QUICK_THINK_LLM=gpt-6-luna TRADINGAGENTS_DEEP_THINK_LLM=gpt-6-sol
export TRADINGAGENTS_OUTPUT_LANGUAGE=English TRADINGAGENTS_MAX_DEBATE_ROUNDS=1 TRADINGAGENTS_MAX_RISK_ROUNDS=1
tradingagents --ticker NVDA --date 2026-09-23 --analysts market,news,fundamentals --save --no-show
```
Each flag skips only its own question. Run without a terminal, a missing answer stops the run before it starts and names the flag or variable to set.

At start-up the interactive CLI mentions a newer release and notices from Tauric Research. It checks at most once a day, in the background, and sends nothing about your runs; set `TRADINGAGENTS_NO_UPDATE_CHECK=1` to turn it off. Runs without a terminal never check.

A saved report also includes `complete_report.html`, the report as one page with its sections listed beside the text, for reading in a browser, on a phone or in print. Answering the save question at the prompt also asks about the page and can open it in your browser; `--no-html` skips it, and so does `ta.save_reports(state, "NVDA", html=False)` from Python.

### Markets and tickers

TradingAgents works with any market Yahoo Finance covers, using the exchange-suffixed ticker. Company identity and the alpha benchmark resolve automatically per market.

- US: `AAPL`, `SPY`
- Hong Kong: `0700.HK` · Tokyo: `7203.T` · London: `AZN.L`
- India: `RELIANCE.NS`, `.BO` · Canada: `.TO` · Australia: `.AX`
- China A-shares: Shanghai `.SS`, Shenzhen `.SZ` (e.g. `600519.SS` for Kweichow Moutai)
- Crypto: `BTC-USD`, `ETH-USD`

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

## TradingAgents Package

### Implementation Details

We built TradingAgents with LangGraph to ensure flexibility and modularity. The framework supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba DashScope, international and China endpoints), GLM (Zhipu), MiniMax (global + China), OpenRouter, Ollama for local models, Azure OpenAI for enterprise, and your own ChatGPT plan.

### Python Usage

To use TradingAgents inside your code, you can import the `tradingagents` module and initialize a `TradingAgentsGraph()` object. The `.propagate()` function will return a decision. You can run `main.py`, here's also a quick example:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
state, decision = ta.propagate("NVDA", "2026-09-01")
print(decision)

# the same report tree the CLI saves, under results_dir/reports
ta.save_reports(state, "NVDA")
```

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # e.g. openai, google, anthropic, deepseek, groq, ollama; openai_compatible covers any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp, ...)
config["deep_think_llm"] = "gpt-6-sol"    # Model for complex reasoning
config["quick_think_llm"] = "gpt-6-luna"   # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-09-01")
print(decision)
```

The quick model serves the analysts, researchers, debaters and trader; the deep model serves the research and portfolio managers. Each can run on its own provider, for example the managers on Claude while the rest run on OpenAI:

```python
config["deep_think_provider"] = "anthropic"
config["deep_think_llm"] = "claude-opus-5-5"
```

A tier on its own provider uses that provider's key and default endpoint; set `quick_think_backend_url` or `deep_think_backend_url` for a local or relay endpoint. The `TRADINGAGENTS_DEEP_THINK_PROVIDER` and `TRADINGAGENTS_QUICK_THINK_PROVIDER` variables set them for the CLI, together with the tier's model variable.

See `tradingagents/default_config.py` for all configuration options.

### Fundamentals as filed

US company statements come from SEC EDGAR, which records the date every figure was filed. A run dated in the past reads the statements exactly as they stood that day: a fiscal year that has ended but has not been filed yet is not served, and a figure restated later still reads as first reported. Apple's 2008 total assets were filed as $39.6B and restated to $36.2B in 2010, so a run dated in between reads $39.6B. EDGAR needs no account or API key.

Other companies' statements come from Yahoo Finance, which dates a statement by the period it covers rather than by when it was published. A run dated today reads them; a run dated in the past is told they are withheld, since Yahoo cannot say which figures were public by then.

Insider trades are dated by when they happened, not when they were filed, so a run dated in the past is told they are withheld as well.

SEC asks callers to identify themselves and refuses requests that carry no contact address, so a default one is sent. Set your own so SEC can reach you rather than the project:

```bash
SEC_EDGAR_USER_AGENT="Your Name your@email.com"
```

It covers companies that file with the SEC, including foreign companies listed in the US. Anything else, such as Hong Kong or A-share listings, falls through to the next vendor in the chain. EDGAR's machine-readable filings begin in 2009, and a fourth quarter is reported as unavailable rather than derived, because filers publish it only inside the annual figure.

### Current holdings

By default the agents do not know what you hold, so their guidance is written for a reader who applies it to their own position. Pass a portfolio to have the trader, the risk analysts and the portfolio manager work against your actual book.

```python
from tradingagents.portfolio import PortfolioContext

portfolio = PortfolioContext.model_validate({
    "cash": 25000.0,
    "currency": "USD",
    "positions": [{"ticker": "NVDA", "quantity": 120, "average_price": 150.0}],
})
_, decision = ta.propagate("NVDA", "2026-09-01", portfolio=portfolio)
```

The CLI takes the same content as a JSON file: `tradingagents --portfolio my_book.json`.

An empty `positions` list means a flat book, which is different from passing nothing. A run without a portfolio is never treated as flat.

## Persistence and Recovery

TradingAgents persists two kinds of state across runs.

### Memory log

The memory log is always on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md`. While the analysts of a later run work, TradingAgents settles every logged decision whose holding period has passed: it fetches the realised return (raw, and alpha against the instrument's regional benchmark) and generates a one-paragraph reflection. The Portfolio Manager then reads the most recent decisions for the same ticker plus recent lessons from other tickers, so each analysis carries forward what worked and what didn't. If settling fails, the run goes on and its report says so.

Override the path with `TRADINGAGENTS_MEMORY_LOG_PATH`.

To settle decisions without running an analysis, for a scheduled job, call `ta.settle_all_pending()`; it returns the decisions it settled and any it could not.

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after each node so a crashed or interrupted run resumes from the last successful step instead of starting over. The run view says whether it resumed a saved run or started fresh. Checkpoints are cleared automatically on successful completion.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db` (override the base with `TRADINGAGENTS_CACHE_DIR`). Use `--clear-checkpoints` to reset all of them before a run.

```bash
tradingagents --checkpoint           # enable for this run
tradingagents --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-09-01")
```

## Evaluating decisions over time

One run gives one decision, which cannot tell you whether the system decides well. `run_backtest` runs the same pipeline over a grid of tickers and dates, writes to a memory log of its own, and scores the decisions whose holding window has since traded.

```python
from tradingagents.backtest import iter_grid, run_backtest, summarize

dates = iter_grid("2026-06-01", "2026-08-01", every_n_days=7)
result = run_backtest(["NVDA", "AAPL"], dates, config, selected_analysts=["market", "news"])
print(summarize(result).render())
```

From the CLI:

```bash
tradingagents backtest NVDA,AAPL --start 2026-06-01 --end 2026-08-01 --every 7
```

Each cell is scored on realized alpha against the instrument's regional benchmark, grouped by rating. A decision is entered at the first close after its date, because a run reads news published after that day's close. Your own memory log is never written to, and re-running the same grid with `run_id=result.run_id` skips the cells that already ran, so an interrupted sweep continues where it stopped.

## Reproducibility

TradingAgents is LLM-driven, so two runs of the same ticker and date can differ. This is expected for a research tool built on language models, not a defect. The variation comes from a few distinct sources, and it helps to separate them.

Language model sampling is non-deterministic. Even at a fixed temperature, providers do not guarantee byte-identical output across calls, and reasoning models (the default GPT-6 family, and any thinking-mode model) vary the most because their internal reasoning is itself sampled.

Live data moves. News, StockTwits, and Reddit return different content as time passes, so a run today sees different inputs than a run last week even for the same historical trade date. Pin the analysis date to hold the price and indicator window fixed, but the social and news sources still reflect "now".

To reduce variation you can lower the sampling temperature. Set `temperature` in your config (or `TRADINGAGENTS_TEMPERATURE` in `.env`); lower values make models that honor it more repeatable. The current curated models are reasoning-first and largely ignore temperature, so for tighter reproducibility name a non-reasoning model in your config, or in `TRADINGAGENTS_DEEP_THINK_LLM` and `TRADINGAGENTS_QUICK_THINK_LLM`. Any model ID your provider serves is accepted, whether or not the picker lists it.

```python
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["temperature"] = 0.0
# Reasoning models ignore temperature. For tighter reproducibility, name a
# non-reasoning model in deep_think_llm / quick_think_llm.
```

What does not vary anymore: the analyzed company identity is resolved deterministically from the ticker before any agent runs, and the market analyst grounds exact price and indicator claims in a verified data snapshot. Earlier reports of "different companies" or fabricated price levels across runs are addressed by these two mechanisms.

Backtest results are not guaranteed to match any published figure. Returns depend on the model, the temperature, the date range, data quality, and the sampling above. Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return.

## Contributing

Contributions are welcome: bug fixes, documentation, and feature ideas; past contributions are credited per release in [`CHANGELOG.md`](CHANGELOG.md).

## Citation

Please reference our work if you find *TradingAgents* provides you with some help :)

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
