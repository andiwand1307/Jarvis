"""Entry point: `python -m jarvis_core`."""

from __future__ import annotations

import logging

import uvicorn

from .config import Config
from .server import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    host = config.bind_host
    if config.access_mode == "local" and config.host and config.host not in ("127.0.0.1", "localhost"):
        logging.getLogger("jarvis").warning(
            "JARVIS_TOKEN ist nicht gesetzt: Jarvis lauscht nur auf 127.0.0.1 (statt %s).", config.host
        )
    logging.getLogger("jarvis").info(
        "Jarvis startet (Modus %s, %s:%s, Modell %s)", config.access_mode, host, config.port, config.model
    )
    uvicorn.run(create_app(config), host=host, port=config.port, proxy_headers=False, log_level="info")


if __name__ == "__main__":
    main()
