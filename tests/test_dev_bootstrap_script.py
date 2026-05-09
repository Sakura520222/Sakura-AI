"""Local dev bootstrap script tests."""

from scripts.dev_bootstrap import find_server_python


def test_find_server_python_prefers_repo_venv_on_windows(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr("scripts.dev_bootstrap.os.name", "nt")

    assert find_server_python(tmp_path, "") == venv_python


def test_find_server_python_allows_explicit_override(tmp_path):
    explicit_python = tmp_path / "custom-python"

    assert find_server_python(tmp_path, str(explicit_python)) == explicit_python
