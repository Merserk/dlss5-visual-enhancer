from __future__ import annotations

"""Process-wide coordination for NVIDIA NGX feature evaluations.

RTX Video, Neural Rendering, and Frame Interpolation load NGX into the same
long-lived Python process. NVIDIA's feature runtimes are not safe to evaluate
concurrently, so all native entry points share this re-entrant lock. Normal
session teardown must not unload NGX or call its core shutdown routine.
"""

import threading


NGX_RUNTIME_LOCK = threading.RLock()
