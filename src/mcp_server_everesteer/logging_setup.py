"""Redacting stderr logging setup (keeps stdout clean for MCP JSON-RPC)."""

import logging
import sys

from mcp_server_everesteer.redaction import scrub


class RedactingFormatter(logging.Formatter):
    """Scrubs known secrets before writing any log record."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def configure_logging(level: str = "INFO") -> None:
    """Configure stderr logging with secret scrubbing and quiet third-party loggers."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for noisy in ("httpx2", "httpcore", "httpcore2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
