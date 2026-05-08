from unittest.mock import AsyncMock

import pytest

from backend.core.config import (
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
)
from backend.services.ai_reviewer.pr_dependency_graph import PRDependencyGraphService
from backend.services.pr_analyzer import PRFileInfo


@pytest.fixture
def service():
    return PRDependencyGraphService(api_client=AsyncMock(), model="test-model")


def make_file(path: str, status: str = "modified") -> PRFileInfo:
    return PRFileInfo(
        path=path,
        status=status,
        additions=1,
        deletions=0,
        changes=1,
        is_code_file=True,
    )


def test_static_mermaid_links_python_imports(service):
    files = [
        make_file("backend/api/users.py"),
        make_file("backend/services/user_service.py"),
    ]
    contents = {
        "backend/api/users.py": "from backend.services.user_service import UserService\n",
        "backend/services/user_service.py": "class UserService:\n    pass\n",
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert graph.startswith("graph TD")
    assert 'N1["backend/api/users.py"]' in graph
    assert 'N2["backend/services/user_service.py"]' in graph
    assert "N1 --> N2" in graph


def test_static_mermaid_links_relative_typescript_imports(service):
    files = [
        make_file("src/components/Button.tsx"),
        make_file("src/utils/theme.ts"),
    ]
    contents = {
        "src/components/Button.tsx": "import { theme } from '../utils/theme';\n",
        "src/utils/theme.ts": "export const theme = {};\n",
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert 'N1["src/components/Button.tsx"]' in graph
    assert 'N2["src/utils/theme.ts"]' in graph
    assert "N1 --> N2" in graph


def test_static_mermaid_generates_nodes_without_edges(service):
    files = [make_file("src/a.py"), make_file("src/b.py")]
    contents = {
        "src/a.py": "import os\n",
        "src/b.py": "import sys\n",
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert graph.startswith("graph TD")
    assert 'N1["src/a.py"]' in graph
    assert 'N2["src/b.py"]' in graph
    assert "-->" not in graph


def test_static_mermaid_respects_max_nodes(service):
    files = [
        make_file("src/a.py"),
        make_file("src/b.py"),
        make_file("src/c.py"),
    ]
    contents = {
        "src/a.py": "from src.b import B\nfrom src.c import C\n",
        "src/b.py": "class B: pass\n",
        "src/c.py": "class C: pass\n",
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=2)

    assert graph.count('["src/') == 2
    assert "OMITTED" in graph


def test_dependency_graph_mode_dynamic_config_registered():
    assert "pr_dependency_graph_mode" in DYNAMIC_CONFIG_GROUPS["pr_dependency_graph"]["keys"]
    assert DYNAMIC_CONFIG_LABELS["pr_dependency_graph_mode"] == "PR 依赖图模式"
    assert DYNAMIC_CONFIG_SELECT_OPTIONS["pr_dependency_graph_mode"] == [
        {"value": "ai", "label": "AI 生成（使用 LLM 分析）"},
        {"value": "static", "label": "静态分析（正则提取 import）"},
    ]
