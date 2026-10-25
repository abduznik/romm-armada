# SPDX-License-Identifier: MIT
"""One logger for the package. main.py hands it Decky's, so lines land in the plugin's log file."""
from __future__ import annotations

import logging

_logger = logging.getLogger("romm_armada")


def set_logger(logger: logging.Logger) -> None:
    global _logger
    _logger = logger


def get() -> logging.Logger:
    return _logger


def info(msg: str, *args) -> None:
    _logger.info(msg, *args)


def warn(msg: str, *args) -> None:
    _logger.warning(msg, *args)


def error(msg: str, *args) -> None:
    _logger.error(msg, *args)


def debug(msg: str, *args) -> None:
    _logger.debug(msg, *args)


def exception(msg: str, *args) -> None:
    _logger.exception(msg, *args)
