"""测试用 Mock 辅助类，供多个测试模块共享。"""

from types import SimpleNamespace


class RequestStub:
    """模拟 FastAPI Request。"""

    def __init__(self, token: str | None = "token"):
        self.cookies = {"webui_token": token} if token else {}
        self.state = SimpleNamespace()


class ResultStub:
    """模拟 SQLAlchemy Result。"""

    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class DbStub:
    """模拟 AsyncSession。"""

    def __init__(self, config):
        self.config = config
        self.execute_count = 0

    async def execute(self, statement):
        self.execute_count += 1
        return ResultStub(self.config)
