"""The only upload handler of the project: in memory, capped at MAX_UPLOAD_BYTES.

Django's default handlers spool uploads over FILE_UPLOAD_MAX_MEMORY_SIZE
(2.5 MB) to a temporary file: the plaintext of a document would be written to
disk before it is encrypted. This handler keeps every upload in memory and
stops keeping data once the REQUEST total exceeds the limit (StopUpload): the
upload is then absent from request.FILES and ``request.upload_too_large`` is
set, for the view to report it. The proxy caps the request body as well
(docs/DEPLOYMENT.md).
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from .validation import MAX_UPLOAD_BYTES


class LimitedMemoryUploadHandler(FileUploadHandler):
    def __init__(self, request: Any = None) -> None:
        super().__init__(request)
        self.received = 0  # whole request, all files
        self.too_large = False
        # Django closes handler.file after StopUpload: always a BytesIO, never None.
        self.file = BytesIO()

    def new_file(self, *args: Any, **kwargs: Any) -> None:
        super().new_file(*args, **kwargs)
        self.file = BytesIO()

    def receive_data_chunk(self, raw_data: bytes, start: int) -> None:
        self.received += len(raw_data)
        if self.received > MAX_UPLOAD_BYTES:
            self.too_large = True
            self.file.close()  # drop what was kept
            self.file = BytesIO()
            if self.request is not None:
                setattr(self.request, "upload_too_large", True)  # noqa: B010 - not a model field
            # connection_reset=False: Django reads and DISCARDS the rest of the
            # body (bounded by the proxy), so the browser gets the error page
            # instead of a reset connection. Nothing more is kept.
            raise StopUpload(connection_reset=False)
        self.file.write(raw_data)

    def file_complete(self, file_size: int) -> InMemoryUploadedFile | None:
        if self.too_large:
            return None
        self.file.seek(0)
        return InMemoryUploadedFile(
            file=self.file,
            field_name=self.field_name,
            name=self.file_name,
            content_type=self.content_type,
            size=file_size,
            charset=self.charset,
            content_type_extra=self.content_type_extra,
        )
