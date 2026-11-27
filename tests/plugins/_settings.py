import os


def seconds(name: str, default: str | None = None) -> float:
    raw = os.environ.get(name, default)
    if raw is None:
        raise RuntimeError(f"this plugin needs {name}=<seconds> in the environment (see tests/plugins/__init__.py)")
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name}={raw!r} is not a number of seconds") from None
