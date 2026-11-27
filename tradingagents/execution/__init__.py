"""F1: human-approved paper trading.

``order_policy`` decides what may be proposed and checks a person's order before
approval (pure, no broker). ``alpaca_paper`` is the only module that talks to
Alpaca, and only to its paper-trading endpoint. The MCP server never imports
this package.
"""
