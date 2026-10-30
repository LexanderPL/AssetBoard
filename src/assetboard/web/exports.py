"""Temporary-file responses shared by ledger and workspace exports."""

from collections.abc import Iterator
from tempfile import SpooledTemporaryFile
from typing import Any

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class SpoolResponse(StreamingResponse):
    """Streams a spooled export file and closes it however the response ends: after the last
    chunk, or when sending fails (the client went away mid-download)."""

    def __init__(self, spool: SpooledTemporaryFile[bytes], **kwargs: Any) -> None:
        def chunks() -> Iterator[bytes]:
            while data := spool.read(EXPORT_CHUNK_BYTES):
                yield data

        super().__init__(chunks(), **kwargs)
        self.spool = spool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.spool.close()


# The export is written to a spooled file first (one consistent read), then streamed.
EXPORT_SPOOL_BYTES = 8 * 1024 * 1024
EXPORT_CHUNK_BYTES = 64 * 1024
