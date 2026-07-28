from pathlib import Path
from types import SimpleNamespace

import run_ruff


class FakeRuffRunner:
    def __init__(self, tmp_path: Path, outcomes: dict[str, bool]):
        self.project_root = tmp_path
        self.venv_python = tmp_path / "python"
        self.logs_dir = tmp_path / "logs"
        self.outcomes = outcomes
        self.calls: list[str] = []

    def check(self) -> bool:
        self.calls.append("check")
        return self.outcomes["check"]

    def fix(self) -> bool:
        self.calls.append("fix")
        return self.outcomes["fix"]

    def format(self) -> bool:
        self.calls.append("format")
        return self.outcomes["format"]

    def check_paths(self, paths: list[str]) -> bool:
        self.calls.append(f"check_paths:{','.join(paths)}")
        return self.outcomes["check_paths"]


def test_full_mode_returns_failure_when_any_step_fails(monkeypatch, tmp_path, capsys):
    runner = FakeRuffRunner(
        tmp_path,
        {"check": True, "fix": True, "format": False},
    )
    monkeypatch.setattr(run_ruff, "RuffRunner", lambda: runner)
    monkeypatch.setattr(run_ruff.sys, "argv", ["run_ruff.py"])

    exit_code = run_ruff.main()

    output = capsys.readouterr().out
    assert exit_code == 1
    assert runner.calls == ["check", "fix", "format"]
    assert "[FAILED] 完整模式执行失败！" in output
    assert "[OK] 完整模式执行完成！" not in output


def test_check_mode_propagates_failure_exit_code(monkeypatch, tmp_path):
    runner = FakeRuffRunner(tmp_path, {"check": False})
    monkeypatch.setattr(run_ruff, "RuffRunner", lambda: runner)
    monkeypatch.setattr(run_ruff.sys, "argv", ["run_ruff.py", "--check"])

    assert run_ruff.main() == 1
    assert runner.calls == ["check"]


def test_full_mode_returns_success_when_every_step_succeeds(
    monkeypatch, tmp_path, capsys
):
    runner = FakeRuffRunner(
        tmp_path,
        {"check": True, "fix": True, "format": True},
    )
    monkeypatch.setattr(run_ruff, "RuffRunner", lambda: runner)
    monkeypatch.setattr(run_ruff.sys, "argv", ["run_ruff.py"])

    exit_code = run_ruff.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert runner.calls == ["check", "fix", "format"]
    assert "[OK] 完整模式执行完成！" in output


def test_check_treats_ruff_traversal_warning_as_failure(monkeypatch, tmp_path):
    runner = run_ruff.RuffRunner(tmp_path)
    monkeypatch.setattr(
        run_ruff.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="All checks passed!\n",
            stderr="warning: Encountered error: 拒绝访问。 (os error 5)\n",
        ),
    )

    assert runner.check() is False


def test_codex_temporary_directory_is_ignored():
    gitignore = Path(__file__).parents[1] / ".gitignore"

    assert ".codex-tmp/" in gitignore.read_text(encoding="utf-8").splitlines()
