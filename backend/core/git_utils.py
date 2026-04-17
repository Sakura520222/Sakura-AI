"""Git 安全工具：使用 GIT_ASKPASS 机制避免在进程参数中暴露访问令牌"""

import os
import sys
import tempfile


def create_git_auth_env(token: str) -> tuple[dict[str, str], str]:
    """创建 Git 认证环境变量，通过 GIT_ASKPASS 传递凭证。

    避免将 token 嵌入 clone URL，防止在进程列表 /proc/PID/cmdline 中泄露。

    Args:
        token: GitHub installation access token

    Returns:
        (env_dict, askpass_path) - 环境变量字典和 askpass 脚本路径
        调用方需在使用后调用 os.unlink(askpass_path) 清理
    """
    if sys.platform == "win32":
        askpass = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bat", delete=False, prefix="sakura_askpass_"
        )
        askpass.write(f"@echo off\necho {token}\n")
    else:
        askpass = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, prefix="sakura_askpass_"
        )
        askpass.write(f"#!{sys.executable}\nprint('{token}')\n")

    askpass.close()

    if sys.platform != "win32":
        os.chmod(askpass.name, 0o755)

    env = {
        **os.environ,
        "GIT_ASKPASS": askpass.name,
        "GIT_TERMINAL_PROMPT": "0",
    }

    return env, askpass.name
