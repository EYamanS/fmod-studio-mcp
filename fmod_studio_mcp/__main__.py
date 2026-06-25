"""Entry point for `python -m fmod_studio_mcp`."""

import asyncio

from .server import main

if __name__ == "__main__":
    asyncio.run(main())
