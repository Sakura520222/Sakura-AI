"""Updater daemon backend — host 侧 daemon 生命周期管理（spec §7.1、§11）。

backend 包 marker；``DaemonBackend`` 是 backend CLI（``backend start/stop/status``）
与 host bootstrap 的统一入口。
"""

from __future__ import annotations

from sakura_ai_updater.backends.daemon import DaemonBackend

__all__ = ["DaemonBackend"]
