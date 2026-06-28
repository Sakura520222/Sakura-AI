#!/usr/bin/env python3
"""
一键 Ruff 代码检查和格式化脚本
支持自动激活 venv，并将日志输出到 logs 目录
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


class RuffRunner:
    """Ruff 运行器"""

    def __init__(self, project_root: Path = None):
        """初始化运行器"""
        self.project_root = project_root or Path(__file__).parent.absolute()
        self.venv_python = self._find_venv_python()
        self.logs_dir = self.project_root / "logs"
        self.logs_dir.mkdir(exist_ok=True)

    def _find_venv_python(self) -> Path:
        """查找 venv 中的 Python 解释器"""
        # 常见的 venv 位置
        venv_paths = [
            self.project_root / "venv",
            self.project_root / ".venv",
            self.project_root / "env",
        ]

        for venv_path in venv_paths:
            if venv_path.exists():
                # 根据操作系统选择 python 可执行文件路径
                if sys.platform == "win32":
                    python_path = venv_path / "Scripts" / "python.exe"
                else:
                    python_path = venv_path / "bin" / "python"

                if python_path.exists():
                    print(f"[OK] 找到 venv: {venv_path}")
                    return python_path

        # 没找到 venv，使用系统 Python
        print("[!] 未找到 venv，使用系统 Python")
        return Path(sys.executable)

    def _run_command(self, cmd: list, description: str) -> tuple[int, str, str]:
        """运行命令并返回结果"""
        print(f"\n{'=' * 60}")
        print(description)
        print(f"{'=' * 60}")
        print(f"命令: {' '.join(cmd)}\n")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=self.project_root,
            )
            return result.returncode, result.stdout, result.stderr
        except Exception as e:
            return 1, "", str(e)

    def _save_log(self, content: str, mode: str) -> Path:
        """保存日志到文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.logs_dir / f"ruff_{mode}_{timestamp}.log"

        with open(log_file, "w", encoding="utf-8") as f:
            f.write(content)

        return log_file

    def check(self) -> bool:
        """检查代码问题（不修改文件）"""
        cmd = [
            str(self.venv_python),
            "-m",
            "ruff",
            "check",
            ".",
            "--output-format=concise",
        ]

        returncode, stdout, stderr = self._run_command(cmd, "检查代码问题")

        # 输出结果
        if stdout:
            print(stdout)
        if stderr:
            print(f"错误信息:\n{stderr}", file=sys.stderr)

        # 保存日志
        log_content = f"""=== Ruff 检查报告 ===
时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
模式: 检查（不修改）

{stdout if stdout else "[OK] 没有发现问题"}
"""
        log_file = self._save_log(log_content, "check")
        print(f"\n日志已保存到: {log_file}")

        return returncode == 0

    def fix(self) -> bool:
        """自动修复问题"""
        cmd = [
            str(self.venv_python),
            "-m",
            "ruff",
            "check",
            ".",
            "--fix",
            "--output-format=concise",
        ]

        returncode, stdout, stderr = self._run_command(cmd, "自动修复问题")

        # 输出结果
        if stdout:
            print(stdout)
        if stderr:
            print(f"错误信息:\n{stderr}", file=sys.stderr)

        # 保存日志
        log_content = f"""=== Ruff 修复报告 ===
时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
模式: 自动修复

{stdout if stdout else "[OK] 没有需要修复的问题"}
"""
        log_file = self._save_log(log_content, "fix")
        print(f"\n日志已保存到: {log_file}")

        return returncode == 0

    def format(self) -> bool:
        """格式化代码"""
        cmd = [str(self.venv_python), "-m", "ruff", "format", "."]

        returncode, stdout, stderr = self._run_command(cmd, "格式化代码")

        # 输出结果
        if stdout:
            print(stdout)
        if stderr:
            print(f"错误信息:\n{stderr}", file=sys.stderr)

        # 保存日志
        log_content = f"""=== Ruff 格式化报告 ===
时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
模式: 格式化

{stdout if stdout else "[OK] 代码已经是格式化的"}
"""
        log_file = self._save_log(log_content, "format")
        print(f"\n日志已保存到: {log_file}")

        return returncode == 0

    def check_paths(self, paths: list) -> bool:
        """检查指定的路径"""
        cmd = [
            str(self.venv_python),
            "-m",
            "ruff",
            "check",
            *paths,
            "--output-format=concise",
        ]

        returncode, stdout, stderr = self._run_command(
            cmd, f"检查指定路径: {' '.join(paths)}"
        )

        # 输出结果
        if stdout:
            print(stdout)
        if stderr:
            print(f"错误信息:\n{stderr}", file=sys.stderr)

        # 保存日志
        log_content = f"""=== Ruff 检查报告 ===
时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
模式: 检查指定路径
路径: {" ".join(paths)}

{stdout if stdout else "[OK] 没有发现问题"}
"""
        log_file = self._save_log(log_content, f"check_{'_'.join(paths)}")
        print(f"\n日志已保存到: {log_file}")

        return returncode == 0


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="一键 Ruff 代码检查和格式化工具（默认执行完整模式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_ruff.py              # 完整模式（检查+修复+格式化）
  python run_ruff.py --check      # 只检查，不修改文件
  python run_ruff.py --check core/  # 检查指定目录
        """,
    )

    parser.add_argument(
        "--check",
        nargs="*",
        metavar="PATH",
        help="检查指定的路径（文件或目录），不带参数时检查整个项目",
    )

    args = parser.parse_args()

    # 创建运行器
    runner = RuffRunner()

    print(f"\n{'=' * 60}")
    print("Ruff 代码检查工具")
    print(f"{'=' * 60}")
    print(f"项目根目录: {runner.project_root}")
    print(f"Python 路径: {runner.venv_python}")
    print(f"日志目录: {runner.logs_dir}")
    print(f"{'=' * 60}\n")

    # 根据参数执行相应的操作
    if args.check is not None:
        # 检查模式
        if args.check:
            # 检查用户指定的路径
            runner.check_paths(args.check)
        else:
            # 没有指定路径，检查整个项目
            runner.check()
    else:
        # 默认：完整模式（检查 → 修复 → 格式化）
        print("\n执行完整模式：检查 → 修复 → 格式化\n")

        # 1. 检查
        runner.check()

        # 2. 修复
        runner.fix()

        # 3. 格式化
        runner.format()

        print("\n" + "=" * 60)
        print("[OK] 完整模式执行完成！")
        print("=" * 60)


if __name__ == "__main__":
    main()
