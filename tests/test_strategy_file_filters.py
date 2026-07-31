"""StrategyConfig 文件过滤规则测试。

锁住 file_filters 的行为契约：哪些文件算代码文件（进入审查）、
哪些应被跳过。覆盖传统代码扩展名以及 3.0.0 重构起纳入审查的
文档/配置类文本文件。
"""

import pytest

from backend.core.config import StrategyConfig


@pytest.fixture
def strategy() -> StrategyConfig:
    return StrategyConfig()


# --- is_code_file：传统代码文件 ---


def test_is_code_file_python(strategy):
    assert strategy.is_code_file("backend/main.py") is True


def test_is_code_file_javascript(strategy):
    assert strategy.is_code_file("src/app.js") is True


# --- is_code_file：文档与配置类文本（3.0.0 起纳入） ---


def test_is_code_file_yaml(strategy):
    assert strategy.is_code_file("config/strategies.yaml") is True


def test_is_code_file_yml(strategy):
    assert strategy.is_code_file("docs/guide.yml") is True


def test_is_code_file_markdown(strategy):
    assert strategy.is_code_file("README.md") is True


def test_is_code_file_json(strategy):
    assert strategy.is_code_file("package.json") is True


def test_is_code_file_toml(strategy):
    assert strategy.is_code_file("pyproject.toml") is True


def test_is_code_file_cfg(strategy):
    assert strategy.is_code_file("conf/app.cfg") is True


def test_is_code_file_ini(strategy):
    assert strategy.is_code_file("settings.ini") is True


def test_is_code_file_txt(strategy):
    assert strategy.is_code_file("CHANGELOG.txt") is True


def test_is_code_file_dockerfile(strategy):
    assert strategy.is_code_file("Dockerfile") is True


def test_is_code_file_dockerfile_in_subdir(strategy):
    assert strategy.is_code_file("docker/Dockerfile") is True


def test_is_code_file_makefile(strategy):
    assert strategy.is_code_file("Makefile") is True


# --- is_code_file：不应算作代码文件 ---


def test_is_code_file_binary_not_code(strategy):
    assert strategy.is_code_file("assets/logo.png") is False


def test_is_code_file_lock_not_code(strategy):
    # .lock 在 skip_extensions，既会被跳过、也不是代码文件
    assert strategy.is_code_file("poetry.lock") is False


def test_is_code_file_unknown_extension_not_code(strategy):
    assert strategy.is_code_file("data.bin") is False


# --- should_skip_file ---


def test_should_skip_file_lock(strategy):
    assert strategy.should_skip_file("poetry.lock") is True


def test_should_skip_file_gitignore(strategy):
    assert strategy.should_skip_file(".gitignore") is True


def test_should_skip_file_license(strategy):
    assert strategy.should_skip_file("LICENSE") is True


def test_should_skip_file_node_modules(strategy):
    assert strategy.should_skip_file("node_modules/lib/index.js") is True


def test_should_skip_file_pycache_at_top_level(strategy):
    # skip_paths 按路径前缀整段匹配：仅匹配顶层 __pycache__/，不匹配嵌套
    # （设计如此，避免 ``.git/`` 误伤 ``.github``；.pyc 也不在 code_extensions）
    assert strategy.should_skip_file("__pycache__/mod.cpython-311.pyc") is True


def test_should_skip_file_python_not_skipped(strategy):
    assert strategy.should_skip_file("backend/main.py") is False


def test_should_skip_file_markdown_not_skipped(strategy):
    assert strategy.should_skip_file("docs/README.md") is False


def test_should_skip_file_yaml_in_node_modules_skipped(strategy):
    # 即便是 yaml，命中 skip_paths 仍应跳过
    assert strategy.should_skip_file("node_modules/pkg/config.yaml") is True
