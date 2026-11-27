"""``tradingagents doctor``: check the setup offline before a run (Phase 3; Phase 2 common errors).

Each check prints OK, WARN or FAIL with what to do. It makes no network request and never prints a key's
value, only whether the variable is set. The problems it catches are the ones the Phase 2 runs hit:

- the wrong Python or virtual environment (``tradingagents`` imported from another install, or packages
  missing because the venv is not active);
- missing packages: the provider's LangChain integration, ``langfuse`` when E3 is on, Streamlit/alpaca-py
  for the dashboard, fastmcp for the MCP server;
- missing credentials: the provider's API key, the Langfuse keys when E3 is on, a backup provider's key;
- E3 pricing that does not cover the configured models (the dashboard then refuses a cost limit);
- model names the provider's catalogue does not list (the usual cause of a Gemini 404);
- on Windows, the PowerShell execution policy that blocks ``Activate.ps1``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROVIDER_PACKAGES = {"google": "langchain_google_genai", "openai": "langchain_openai", "azure": "langchain_openai",
                     "anthropic": "langchain_anthropic", "bedrock": "langchain_aws"}
INSTALL_HINT = {"langchain_aws": 'pip install "tradingagents[bedrock]"', "langfuse": 'pip install "tradingagents[e3]"',
                "streamlit": 'pip install "tradingagents[dashboard]"', "alpaca": 'pip install "tradingagents[dashboard]"',
                "fastmcp": 'pip install "tradingagents[mcp]"'}


@dataclass
class Check:
    level: str          # OK | WARN | FAIL
    name: str
    detail: str


def _windows() -> bool:
    return os.name == "nt"


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _install(module: str) -> str:
    return INSTALL_HINT.get(module, 'pip install -e .   (from the project folder, with its venv active)')


def run_checks(config: dict | None = None, environ: dict | None = None, project_root: Path | None = None) -> list[Check]:
    from tradingagents.default_config import DEFAULT_CONFIG

    env = os.environ if environ is None else environ
    config = dict(DEFAULT_CONFIG if config is None else config)
    root = project_root or Path(__file__).resolve().parents[1]
    checks: list[Check] = []
    add = lambda level, name, detail: checks.append(Check(level, name, detail))  # noqa: E731

    # 1. Interpreter and environment
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    add("OK" if sys.version_info >= (3, 10) else "FAIL", "python",
        f"{sys.version.split()[0]} at {sys.executable}" + ("" if in_venv else " (no virtual environment active)"))
    if not in_venv:
        add("WARN", "virtualenv", "no virtual environment is active: packages may come from another install. "
            + ("Run .venv\\Scripts\\python.exe -m cli.main (or activate it)" if _windows()
               else "Run .venv/bin/python -m cli.main, or `source .venv/bin/activate`"))
    import tradingagents

    here = Path(tradingagents.__file__).resolve().parents[1]
    if here != root.resolve():
        add("FAIL", "install", f"tradingagents is imported from {here}, not this project ({root}): the wrong "
            "virtual environment is active, or an older copy is installed. Use this project's venv and pip install -e .")
    else:
        add("OK", "install", f"tradingagents from {here}")
    if _windows():
        add("WARN", "powershell", "if activating fails with 'running scripts is disabled on this system', run "
            "`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or skip activation and call "
            ".venv\\Scripts\\python.exe directly")

    # 2. Packages
    for module in ("langchain_core", "langgraph", "yfinance", "dotenv"):
        add("OK" if _has(module) else "FAIL", f"package {module}", "installed" if _has(module) else _install(module))
    providers = {(config.get("llm_provider") or "").lower()}
    for tier in ("quick", "deep"):
        for key in (f"{tier}_think_provider", f"{tier}_fallback_provider"):
            if config.get(key):
                providers.add(str(config[key]).lower())
    for provider in sorted(p for p in providers if p):
        module = PROVIDER_PACKAGES.get(provider, "langchain_openai")
        add("OK" if _has(module) else "FAIL", f"provider {provider}",
            f"{module} installed" if _has(module) else f"{module} missing: {_install(module)}")

    # 3. Credentials (names only, never values)
    from tradingagents.llm_clients.api_key_env import get_api_key_env

    dotenv = root / ".env"
    add("OK" if dotenv.is_file() else "WARN", ".env", str(dotenv) if dotenv.is_file()
        else f"no .env in {root}: keys must come from the environment (copy .env.example to .env)")
    for provider in sorted(p for p in providers if p):
        key = get_api_key_env(provider)
        if key:
            add("OK" if (env.get(key) or "").strip() else "FAIL", f"key {key}",
                "set" if (env.get(key) or "").strip() else f"not set: add {key}=... to .env for {provider}")

    # 4. E3
    e3_on = (env.get("E3_TRACE_ENABLED") or "").lower() in {"1", "true", "yes"}
    if e3_on:
        add("OK" if _has("langfuse") else "FAIL", "package langfuse",
            "installed" if _has("langfuse") else f"E3_TRACE_ENABLED=1 needs it: {_install('langfuse')}, or set E3_TRACE_ENABLED=0")
        missing = [k for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL") if not env.get(k)]
        add("FAIL" if missing else "OK", "E3 credentials",
            ("missing: " + ", ".join(missing) + " (from your Langfuse project's settings), or set E3_TRACE_ENABLED=0")
            if missing else "LANGFUSE_* set")
    pricing_path = env.get("E3_PRICING_JSON")
    models = {config.get(k) for k in ("quick_think_llm", "deep_think_llm", "quick_fallback_llm", "deep_fallback_llm")}
    models.discard(None)
    if pricing_path:
        path = Path(pricing_path) if Path(pricing_path).is_absolute() else root / pricing_path
        try:
            prices = json.loads(path.read_text(encoding="utf-8"))
            unpriced = sorted(m for m in models if m not in prices)
            add("WARN" if unpriced else "OK", "E3 pricing", f"no price for {', '.join(unpriced)} in {path.name}"
                if unpriced else f"{path.name} covers every configured model")
        except (OSError, ValueError) as exc:
            add("FAIL", "E3 pricing", f"E3_PRICING_JSON={pricing_path} cannot be read: {exc}")
    else:
        add("WARN", "E3 pricing", "E3_PRICING_JSON not set: no cost estimates, and the dashboard refuses a cost limit")

    # 5. Models and backups
    from tradingagents.e1_routing import check_backup
    from tradingagents.llm_clients.validators import validate_model

    provider = (config.get("llm_provider") or "").lower()
    for tier in ("quick", "deep"):
        model = config.get(f"{tier}_think_llm")
        tier_provider = (config.get(f"{tier}_think_provider") or provider).lower()
        if model and not validate_model(tier_provider, model):
            add("WARN", f"model {tier}", f"{model!r} is not in the {tier_provider} catalogue: if the provider "
                "answers 404 NOT_FOUND, the name is wrong or retired")
        result = check_backup(config, tier, environ=env)
        for error in result["errors"]:
            add("FAIL", f"backup {tier}", error)
        for warning in result["warnings"]:
            add("WARN", f"backup {tier}", warning)
    return checks


def main() -> int:
    from tradingagents.default_config import DEFAULT_CONFIG

    checks = run_checks(DEFAULT_CONFIG)
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"{c.level:4}  {c.name:{width}}  {c.detail}")
    failed = sum(c.level == "FAIL" for c in checks)
    print(f"\n{failed} problem(s), {sum(c.level == 'WARN' for c in checks)} warning(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
