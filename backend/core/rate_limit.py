"""Shared application rate limiter."""

import warnings

from slowapi import Limiter
from slowapi.util import get_remote_address

# config_filename="" 阻止 slowapi 读取根目录 .env（Windows 下曾因 GBK 解码报错）；
# starlette 会对该空文件名发 "not found" 警告，仅在此局部屏蔽。
# config_filename="" stops slowapi from loading a root .env (Windows GBK decode
# errors); starlette warns "not found" for the empty name, so scope it here.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="Config file '' not found", category=UserWarning
    )
    limiter = Limiter(key_func=get_remote_address, config_filename="")
