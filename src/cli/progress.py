"""Compact console progress, using the same whole-file updates as the desktop."""
from __future__ import annotations

import re
import shutil
import threading
import time


class ConsoleProgress:
    def __init__(self, paths, stream, *, quiet=False):
        self.paths = paths
        self.stream = stream
        self.quiet = quiet
        self.interactive = bool(stream.isatty())
        self.last_print = 0.0
        self.current_index = None
        self.last_stage = ""
        self.line_open = False
        self.lock = threading.RLock()

    def _line(self, text):
        if self.line_open:
            self.stream.write("\r\033[2K")
            self.line_open = False
        self.stream.write(text + "\n")
        self.stream.flush()

    def __call__(self, update):
        with self.lock:
            if update.index is None:
                self.finish()
                return
            number = f"[{update.index + 1}/{len(self.paths)}]"
            name = self.paths[update.index].name
            if update.state in {"Failed", "Cancelled"}:
                self._line(f"{number} {update.state}: {name}: {update.detail}")
            elif update.state == "Complete":
                if not self.quiet:
                    self._line(f"{number} Saved: {update.output_path}")
            elif update.state == "Running" and not self.quiet:
                match = re.search(r"\bStage (\d+) of (\d+)\b", update.detail)
                stage = f"Processing Stage {match[1]} of {match[2]}" if match else "Preparing"
                part = re.search(r"\bPart (\d+)\b", update.detail)
                if match and part:
                    stage += f" (Part {part[1]})"
                if self.current_index != update.index:
                    self._line(f"{number} {name}")
                    self.current_index = update.index
                    self.last_stage = ""
                if not self.interactive:
                    if stage != self.last_stage:
                        self._line(f"  {stage}")
                        self.last_stage = stage
                    return
                now = time.monotonic()
                if now - self.last_print < .15 and stage == self.last_stage:
                    return
                self.last_print, self.last_stage = now, stage
                fraction = max(0.0, min(.99, update.progress))
                remaining = ""
                if update.elapsed_seconds >= 1 and fraction > .01:
                    seconds = round(update.elapsed_seconds * (1 - fraction) / fraction)
                    remaining = f" | ~{seconds // 60:02d}:{seconds % 60:02d} remaining"
                filled = int(fraction * 20)
                bar = "#" * filled + "-" * (20 - filled)
                text = f"{number} [{bar}] {fraction:.0%} | {stage}{remaining}"
                width = max(20, shutil.get_terminal_size((100, 25)).columns - 1)
                self.stream.write("\r\033[2K" + text[:width])
                self.stream.flush()
                self.line_open = True

    def finish(self):
        with self.lock:
            if self.line_open:
                self.stream.write("\n")
                self.stream.flush()
                self.line_open = False
