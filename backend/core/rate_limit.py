"""Shared application rate limiter."""

from slowapi import Limiter
from slowapi.util import get_remote_address

# config_filename="" skips .env reading and avoids Windows GBK encoding issues.
limiter = Limiter(key_func=get_remote_address, config_filename="")
