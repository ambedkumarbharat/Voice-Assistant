"""ASGI entrypoint: `uvicorn app.main:app`."""

from __future__ import annotations

import logging

from .api import create_app
from .config import get_settings

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
for warning in settings.warnings:
    logging.getLogger("voxdesk").warning(warning)

app = create_app(settings=settings)
