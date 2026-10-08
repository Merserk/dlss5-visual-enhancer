"""Control-only protocol for a stage-owned decoder, model, cache and encoder."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from ..core import app_log
from ..core.jobs import BoundedLogBuffer, Paused, drain_bounded_text
from .rolling_worker import StageProcess


def run_request(payload, directory, controller, *, on_event=None, timeout=300, output_cache=None, input_cache=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    request = directory / "pass-request.json"
    request.write_text(json.dumps({**payload, "protocol": "descriptor-v2",
                                  "trace_stalls": bool(os.environ.get("VE_CACHE_TRACE"))}, default=str), encoding="utf-8")
    process = logger = reader = None
    logs = BoundedLogBuffer(max_tail=70)
    events = queue.Queue(maxsize=32)
    result = None
    failure = None
    label = payload.get("stage", payload.get("operation", "probe"))
    started = heartbeat = time.perf_counter()
    pause_epoch = controller.pause_epoch
    def read():
        try:
            for line in process.stdout:
                event = json.loads(line)
                # Progress is throttled by the worker; backpressure is bounded.
                while True:
                    try:
                        events.put(event, timeout=.1)
                        break
                    except queue.Full:
                        if process.poll() is not None:
                            return
        except BaseException as exc:
            events.put({"event": "protocol-error", "message": str(exc)})
    try:
        controller.check()
        process = StageProcess(request, directory)
        controller.register(process)
        logger = threading.Thread(target=drain_bounded_text, args=(process.stderr, logs), daemon=True)
        reader = threading.Thread(target=read, name="rolling-pass-control", daemon=True)
        logger.start()
        reader.start()
        process.stdin.write(b"\x01")
        process.stdin.flush()
        app_log.info("rolling-cache", f"load stage={label} pid={process.pid}")
        while process.poll() is None or not events.empty() or reader.is_alive():
            if process.poll() is not None:
                # A child may still hold the protocol pipe after root exit.
                # Release the complete tree before waiting for its final EOF.
                process.wait(timeout=20)
            if controller.pause_epoch != pause_epoch:
                raise Paused("Replay the interrupted stage after pause.")
            controller.check()
            try:
                event = events.get(timeout=.05)
            except queue.Empty:
                if time.perf_counter() - heartbeat > timeout:
                    raise TimeoutError(f"{label} worker made no progress for {timeout:g} seconds.")
                continue
            heartbeat = time.perf_counter()
            if event.get("event") == "model-loaded" and int(event.get("instances", 0)) > 1:
                raise RuntimeError("A pass attempted to keep more than one active AI model.")
            if event.get("event") in {"cache-allocate", "cache-retire", "cache-commit"}:
                if event["event"] == "cache-allocate":
                    if output_cache is None:
                        raise RuntimeError("A stage requested cache space without an output owner.")
                    response = dict(mapping=output_cache.allocate(event["index"]))
                elif event["event"] == "cache-retire":
                    if input_cache is None:
                        raise RuntimeError("A stage requested retirement without an input owner.")
                    input_cache.retire(event["index"])
                    response = dict(retired=True)
                    if event.get("acknowledge") is False:
                        continue
                else:
                    if output_cache is None:
                        raise RuntimeError("A stage committed a cache without an output owner.")
                    output_cache.commit(event["count"], history_start=event["history_start"],
                                        history_count=event["history_count"])
                    response = dict(committed=True)
                process.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
                process.stdin.flush()
                continue
            if event.get("event") == "result":
                result = event["result"]
            elif event.get("event") == "protocol-error":
                raise RuntimeError("Stage control protocol failed: " + event["message"])
            if on_event:
                on_event(event)
        process.wait(timeout=20)
        logger.join(timeout=2)
        if controller.pause_epoch != pause_epoch:
            raise Paused("Replay the interrupted stage after pause.")
        controller.check()
        if process.returncode or result is None:
            raise RuntimeError(f"{label} worker failed:\n" + "\n".join(logs.snapshot()))
        result["worker"] = dict(pid=process.pid, exit_code=process.returncode,
                                 active_processes=process.active_processes,
                                 scope_seconds=time.perf_counter() - started)
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if process:
            process.kill()
            try:
                process.wait(timeout=20)
            finally:
                process.close_job()
                controller.unregister(process)
            if reader:
                reader.join(timeout=2)
            if logger:
                logger.join(timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if not pipe.closed:
                    pipe.close()
            app_log.info("rolling-cache", f"unload stage={label} pid={process.pid}")
            if failure is not None:
                stats = getattr(controller, "render_stats", None)
                if stats is not None:
                    evidence = dict(stage=label, pid=process.pid, error=str(failure),
                                    stderr=logs.snapshot(), active_processes=0, exit_code=process.returncode)
                    stats.setdefault("interrupted_workers", []).append(evidence)
                    (directory.parent.parent / "last-pass-error.json").write_text(
                        json.dumps(evidence, indent=2), encoding="utf-8")
        request.unlink(missing_ok=True)


def isolated_probe(operation, ai_gpu_uuid="auto", controller=None):
    import tempfile
    from ..core.jobs import JobController, current_job_controller
    from ..core.paths import JOBS
    controller = controller or current_job_controller() or JobController()
    JOBS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-probe-", dir=JOBS) as directory:
        return run_request(dict(operation=operation, ai_gpu_uuid=ai_gpu_uuid), directory, controller)
