"""Kept so ``from cli.stats_handler import StatsCallbackHandler`` keeps working.

Phase 3 moved the handler to ``tradingagents.run_stats`` so the shared analysis
service (used by the CLI, the dashboard and the MCP server) does not import the CLI.
"""

from tradingagents.run_stats import StatsCallbackHandler

__all__ = ["StatsCallbackHandler"]
