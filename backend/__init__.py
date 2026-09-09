"""Sakura AI Backend"""

import os

__version__ = "3.2.0"

# python-telegram-bot ≥22.2 正把时间类属性从 int 迁往 datetime.timedelta
# （v23 起的默认），旧形态每次访问都会发出 PTBDeprecationWarning。这里在包
# 加载时提前启用新语义；消费方统一经 notification_service.
# normalize_retry_after 归一为秒，两种形态等价。setdefault 保留运维显式覆
# 盖 / opt into PTB's upcoming timedelta semantics at package load time:
# consumers normalize to seconds either way, and explicit operator overrides
# are respected.
os.environ.setdefault("PTB_TIMEDELTA", "1")
