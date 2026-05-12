"""Basic smoke tests for MCP server modules."""

import importlib


def test_calculator_module_loads():
    """Calculator server module should import without errors."""
    mod = importlib.import_module("servers.calculator")
    assert hasattr(mod, "mcp")
    assert hasattr(mod, "evaluate")
    assert hasattr(mod, "run")


async def test_calculator_evaluate():
    """Calculator evaluate should return correct results."""
    from servers.calculator import evaluate

    # FastMCP wraps the function; access the underlying fn
    fn = evaluate.fn if hasattr(evaluate, "fn") else evaluate

    result = await fn("add", 2.0, 3.0)
    assert result == "5.0"

    result = await fn("multiply", 4.0, 5.0)
    assert result == "20.0"

    result = await fn("divide", 10.0, 3.0)
    assert "3.333" in result
