from __future__ import annotations

import threading
import secrets
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class _NoCacheHandler(SimpleHTTPRequestHandler):
    """Serve a Live HLS dir on loopback; never cache playlists."""

    def __init__(self, *args, directory: str, session_token: str, **kwargs) -> None:
        self._session_root = Path(directory).resolve()
        self._session_token = session_token
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        if self.path.split("?")[0].casefold().endswith(".m3u8"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, *args) -> None:  # noqa: ANN002, ANN003 - quiet by design
        return

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Players routinely cancel segment prefetches when seeking/exiting.
            pass

    def send_head(self):
        # Native players use the generated loopback URL. Reject foreign browser
        # origins/Host headers and require the private session URL for GET/HEAD.
        host = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        if self.headers.get("Host") != host or (origin and origin != f"http://{host}"):
            self.send_error(403)
            return None
        try:
            request = urlsplit(self.path)
            parts = request.path.split("/", 2)
            permitted = (not request.scheme and not request.netloc and len(parts) == 3 and
                         secrets.compare_digest(parts[1].encode("utf-8"),
                                                self._session_token.encode("ascii")))
        except (UnicodeError, ValueError):
            permitted = False
        if not permitted:
            self.send_error(404)
            return None
        try:
            relative = unquote(parts[2], errors="strict")
            resolved = (self._session_root / relative).resolve()
            resolved.relative_to(self._session_root)
            available = resolved.is_file() and not resolved.name.casefold().endswith(".tmp")
        except (OSError, ValueError):
            available = False
        if not available:
            self.send_error(404)
            return None
        self.path = "/" + parts[2] + ("?" + request.query if request.query else "")
        return super().send_head()


class HlsServer:
    """Loopback HTTP server for one Live session directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory).resolve()
        self._session_token = secrets.token_hex(16)
        handler = partial(_NoCacheHandler, directory=str(self.directory), session_token=self._session_token)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="dlss5-live-hls",
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def playlist_url(self, name: str = "index.m3u8") -> str:
        return f"http://127.0.0.1:{self.port}/{self._session_token}/{name}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        self._thread.join(timeout=5)
