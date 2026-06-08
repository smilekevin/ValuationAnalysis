from __future__ import annotations

from collections.abc import Callable


ProgressCallback = Callable[[str, str], None]
