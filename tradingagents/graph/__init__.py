"""TradingAgents graph components with lazy public imports."""

from importlib import import_module

__all__ = [
    "TradingAgentsGraph",
    "ConditionalLogic",
    "GraphSetup",
    "Propagator",
]

_LAZY_IMPORTS = {
    "TradingAgentsGraph": ".trading_graph",
    "ConditionalLogic": ".conditional_logic",
    "GraphSetup": ".setup",
    "Propagator": ".propagation",
}


def __getattr__(name):
    if name not in _LAZY_IMPORTS:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    module = import_module(_LAZY_IMPORTS[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
