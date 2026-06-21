from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Callable


LOGGER_NAMESPACE = "satsoc"
CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("satsoc_current_run_id", default=None)
ALLOWED_LEVELS = {logging.DEBUG, logging.INFO, logging.ERROR}

_configured = False
_resolver: Callable[[str], Path] | None = None


class _RunContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "run_id", None):
            record.run_id = CURRENT_RUN_ID.get() or "-"
        return record.levelno in ALLOWED_LEVELS


class _PerRunFileRouter(logging.Handler):
    def __init__(self) -> None:
        super().__init__()

    def _write(self, file_path: Path, record: logging.LogRecord) -> None:
        if self.formatter is None:
            return

        line = self.format(record)
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno not in ALLOWED_LEVELS:
            return

        run_id = str(getattr(record, "run_id", None) or CURRENT_RUN_ID.get() or "").strip()

        try:
            target_log_dirs: list[Path] = []
            if run_id and run_id != "-":
                if _resolver is None:
                    raise RuntimeError("Run directory resolver is not configured.")

                execution_dir = _resolver(run_id)
                log_dir = execution_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                target_log_dirs.append(log_dir)

            for log_dir in target_log_dirs:
                if record.levelno == logging.ERROR:
                    self._write(log_dir / "error.log", record)
                else:
                    self._write(log_dir / "execution.log", record)
        except Exception:
            self.handleError(record)


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(LOGGER_NAMESPACE)

    if name.startswith(f"{LOGGER_NAMESPACE}."):
        return logging.getLogger(name)

    return logging.getLogger(f"{LOGGER_NAMESPACE}.{name}")


def set_current_run_id(run_id: str | None) -> Token:
    return CURRENT_RUN_ID.set(run_id)


def reset_current_run_id(token: Token) -> None:
    CURRENT_RUN_ID.reset(token)


def configure_logging(
    run_dir_resolver: Callable[[str], Path],
) -> logging.Logger:
    global _configured, _resolver

    logger = logging.getLogger(LOGGER_NAMESPACE)
    if _configured:
        return logger

    _resolver = run_dir_resolver

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | run=%(run_id)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_RunContextFilter())

    router_handler = _PerRunFileRouter()
    router_handler.setLevel(logging.DEBUG)
    router_handler.setFormatter(formatter)
    router_handler.addFilter(_RunContextFilter())

    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(console_handler)
    logger.addHandler(router_handler)

    _configured = True
    return logger
