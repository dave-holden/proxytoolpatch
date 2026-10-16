"""toolpatch: LLM tool-calling template-mismatch detector and repairer."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

try:
    __version__: str = _metadata_version("toolpatch")
except PackageNotFoundError:  # unbuilt dev checkout
    __version__ = "0.0.0.dev0+local"

from toolpatch.extractor import ExtractedToolCalls, extract_tool_calls
from toolpatch.fixer import heal_json
from toolpatch.logging import NullLogger, configure_logger, get_logger
from toolpatch.request import normalize_chat_request
from toolpatch.stream import StreamHealer


def _configure_logging(*, level: str | None = None, quiet: bool = False) -> None:
    """Thin wrapper so proxytool can drive toolpatch logging without importing
    the stdlib logger setup inside toolpatch itself.

    ``level`` uses the same tier strings as proxytool: ``debug``, ``info``,
    ``warning``, or ``error``. When ``level`` is ``None``, toolpatch reverts
    to its internal no-op logger.
    """
    from toolpatch.logging import LogLevels
    from toolpatch.logging import configure_logger as _cfg

    _valid: dict[str, LogLevels] = {
        "debug": "debug", "info": "info", "warning": "warning", "error": "error",
    }
    _level: LogLevels | None = None
    if quiet:
        _level = "error"
    elif level:
        _level = _valid.get(level)
    _cfg("toolpatch", _level)

__all__ = [
    "ExtractedToolCalls",
    "NullLogger",
    "StreamHealer",
    "__version__",
    "configure_logger",
    "extract_tool_calls",
    "get_logger",
    "heal_json",
    "normalize_chat_request",
]


