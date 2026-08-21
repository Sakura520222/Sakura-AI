"""Sakura AI Host Updater — 宿主机独立更新编排进程。

仓库内独立 Python 项目（src layout）。dev 模式 ``python -m sakura_ai_updater --serve``；
Slice 3c 再 PyInstaller 打包为单二进制（spec §16.1）。
"""

__version__ = "0.2.0"

# IPC 协议版本（spec §7.2 body envelope）。Slice 3a 实现 v1。
PROTOCOL_VERSION = 1
