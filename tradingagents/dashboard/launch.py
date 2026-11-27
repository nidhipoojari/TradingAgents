"""``tradingagents-dashboard``: start the Streamlit page on this machine only.

The page can place paper orders, so it serves 127.0.0.1 only: a non-loopback
``--server.address`` is refused. Command-line flags take precedence over
Streamlit's environment variables and config files, so the loopback address
passed here always wins. Other arguments pass through to ``streamlit run``.
"""

import sys
from pathlib import Path

from tradingagents.mcp_server import is_loopback


def server_address(args: list[str]) -> str | None:
    """The ``--server.address`` given on the command line, if any."""
    for i, arg in enumerate(args):
        if arg.startswith("--server.address="):
            return arg.split("=", 1)[1]
        if arg == "--server.address":
            return args[i + 1] if i + 1 < len(args) else ""
    return None


def main() -> None:
    args = sys.argv[1:]
    address = server_address(args)
    if address is not None and not is_loopback(address):
        sys.exit(f"tradingagents-dashboard serves this machine only; {address!r} is not a loopback address. "
                 "Use 127.0.0.1 or localhost.")
    try:
        from streamlit.web import cli as streamlit_cli
    except ImportError:
        sys.exit('The dashboard needs its extra: pip install ".[dashboard]"')
    app = Path(__file__).with_name("app.py")
    if address is None:
        args = ["--server.address=127.0.0.1", *args]
    sys.argv = ["streamlit", "run", str(app), *args]
    sys.exit(streamlit_cli.main())


if __name__ == "__main__":
    main()
