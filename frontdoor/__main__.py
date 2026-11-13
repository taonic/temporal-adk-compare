"""Run the front door: the demo on PORT (8080), the Temporal UI on UI_PORT (8081).

    DAYTONA_API_KEY=... uv run --group frontdoor python -m frontdoor
"""

import asyncio
import os

import uvicorn

from .app import app, ui


async def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    servers = [uvicorn.Server(uvicorn.Config(application, host=host, port=port, log_level="warning", proxy_headers=True,
                                             forwarded_allow_ips="*"))
               for application, port in ((app, int(os.environ.get("PORT", 8080))), (ui, int(os.environ.get("UI_PORT", 8081))))]
    print(f"Front door: the demo on :{servers[0].config.port}, the Temporal UI on :{servers[1].config.port}", flush=True)
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
