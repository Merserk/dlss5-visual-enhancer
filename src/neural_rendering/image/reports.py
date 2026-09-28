from __future__ import annotations

import os
import zipfile
from pathlib import Path

from ...core import app_log
from ...core.disk_paths import OutputFile
from ...core.jobs import Cancelled
from .models import ImageConversionResult


def record_image_result(result: ImageConversionResult) -> str:
    """Record completion and return the shared session log path."""
    app_log.info(
        "image-render",
        f"done src={Path(result.input_path).name} out={Path(result.output_path).name} "
        f"elapsed={result.elapsed_seconds:.1f}s",
    )
    return app_log.session_path()


class IncrementalImageArchive:
    """Build the uncompressed batch ZIP while later images are on the GPU."""

    def __init__(self, path: Path, controller=None):
        self.path = path
        self.controller = controller
        self.output_file = OutputFile(path)
        self.archive = zipfile.ZipFile(
            self.output_file.temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True,
        )
        self.closed = False
        self.error: str | None = None
        self.count = 0

    def add(self, source_path: str | os.PathLike[str]) -> None:
        if self.closed or self.error:
            return
        source = Path(source_path)
        try:
            info = zipfile.ZipInfo.from_file(source, arcname=source.name)
            info.compress_type = zipfile.ZIP_STORED
            with source.open("rb") as input_stream, self.archive.open(info, "w", force_zip64=True) as output_stream:
                while True:
                    if self.controller is not None and self.controller.cancel.is_set():
                        raise Cancelled("ZIP creation cancelled.")
                    chunk = input_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output_stream.write(chunk)
            self.count += 1
        except Cancelled:
            raise
        except Exception as exc:
            # ZIP is a convenience artifact; never turn an already valid image into
            # a failed render because archive I/O failed.
            self.error = str(exc)

    def finish(self, *, cancelled: bool) -> str | None:
        if self.closed:
            return None
        self.closed = True
        try:
            try:
                self.archive.close()
            except Exception as exc:
                self.error = self.error or str(exc)
            if not self.count or cancelled or self.error or (self.controller is not None and self.controller.cancel.is_set()):
                return None
            self.output_file.publish()
            return str(self.path)
        finally:
            self.output_file.cleanup()

    def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            try:
                self.archive.close()
            except Exception:
                pass
        finally:
            self.output_file.cleanup(rollback=True)
