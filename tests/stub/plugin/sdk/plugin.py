"""Minimal stand-in for ``plugin.sdk.plugin`` used by the smoke tests.

It implements only the surface the birthday plugin touches, so the plugin can
be imported and exercised without a running N.E.K.O. host.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("stub-plugin")


class SdkError(Exception):
    pass


class Ok:
    def __init__(self, value: Any = None):
        self.value = value


class Err:
    def __init__(self, error: Any = None):
        self.error = error


class NekoPluginBase:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.logger = logger

    def data_path(self, *parts: str) -> Path:
        return Path(self.ctx.data_dir).joinpath(*parts)

    def push_message(self, **kwargs: Any) -> dict[str, Any]:
        self.ctx.pushed.append(kwargs)
        return {"submitted": True}

    @property
    def bus(self) -> Any:
        return getattr(self.ctx, "bus", None)


def _identity(fn: Callable[..., Any]) -> Callable[..., Any]:
    return fn


def neko_plugin(cls):
    return cls


def lifecycle(**_kwargs):
    return _identity


def timer_interval(**_kwargs):
    return _identity


def message(**_kwargs):
    return _identity


def plugin_entry(**_kwargs):
    return _identity


def llm_tool(**_kwargs):
    return _identity


class _Ui:
    @staticmethod
    def context(**_kwargs):
        return _identity

    @staticmethod
    def action(**_kwargs):
        return _identity


ui = _Ui()
