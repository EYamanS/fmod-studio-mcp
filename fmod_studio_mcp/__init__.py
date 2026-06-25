"""fmod-studio-mcp — an MCP server that drives FMOD Studio live via its
scripting terminal (TCP 3663)."""

__version__ = "0.1.0"


def run_server() -> None:
    """Console-script entry point."""
    import asyncio

    from .server import main

    asyncio.run(main())
