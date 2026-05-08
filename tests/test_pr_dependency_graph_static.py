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


def test_static_mermaid_links_python_parent_relative_imports(service):
    files = [
        make_file("pkg/features/views.py"),
        make_file("pkg/utils/helper.py"),
    ]
    contents = {
        "pkg/features/views.py": "from ..utils import helper\n",
        "pkg/utils/helper.py": "def run():\n    pass\n",
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert 'N1["pkg/features/views.py"]' in graph
    assert 'N2["pkg/utils/helper.py"]' in graph
    assert "N1 --> N2" in graph


def test_static_mermaid_links_at_alias_imports(service):
    # Static mode treats @/ as a source-root suffix convention. It does not read
    # tsconfig/jsconfig paths, so this resolves because components/Header is a
    # suffix alias of src/components/Header.tsx.
    files = [
        make_file("src/pages/Home.tsx"),
        make_file("src/components/Header.tsx"),
    ]
    contents = {
        "src/pages/Home.tsx": "import Header from '@/components/Header';\n",
        "src/components/Header.tsx": "export default function Header() { return null; }\n",
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert 'N1["src/pages/Home.tsx"]' in graph
    assert 'N2["src/components/Header.tsx"]' in graph
    assert "N1 --> N2" in graph


def test_static_mermaid_links_go_imports(service):
    files = [
        make_file("cmd/app/main.go"),
        make_file("internal/config/config.go"),
    ]
    contents = {
        "cmd/app/main.go": 'package main\n\nimport "internal/config"\n',
        "internal/config/config.go": "package config\n",
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert 'N1["cmd/app/main.go"]' in graph
    assert 'N2["internal/config/config.go"]' in graph
    assert "N1 --> N2" in graph


def test_static_mermaid_links_java_imports(service):
    files = [
        make_file("src/main/java/com/example/App.java"),
        make_file("src/main/java/com/example/service/UserService.java"),
    ]
    contents = {
        "src/main/java/com/example/App.java": (
            "package com.example;\n"
            "import com.example.service.UserService;\n"
        ),
        "src/main/java/com/example/service/UserService.java": (
            "package com.example.service;\n"
        ),
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert 'N1["src/main/java/com/example/App.java"]' in graph
    assert 'N2["src/main/java/com/example/service/UserService.java"]' in graph
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


def test_normalize_path_only_removes_leading_current_dir_segments():
    assert PRDependencyGraphService._normalize_path("./test.") == "test."
    assert PRDependencyGraphService._normalize_path("mymodule/./config.py") == (
        "mymodule/./config.py"
    )
    assert PRDependencyGraphService._normalize_path(".hidden/config.py") == (
        ".hidden/config.py"
    )


def test_escape_mermaid_label_handles_special_characters(service):
    files = [make_file('src/components/<Widget>{v1}|(#%)".tsx')]
    contents = {'src/components/<Widget>{v1}|(#%)".tsx': "export const widget = null;\n"}

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert "&lt;Widget&gt;" in graph
    assert "&#123;v1&#125;&#124;&#40;&#35;&#37;&#41;'" in graph
    assert '".tsx' not in graph


def test_normalize_import_handles_empty_and_dot_edges():
    assert PRDependencyGraphService._normalize_import("pkg/mod.py", "") == set()
    assert PRDependencyGraphService._normalize_import("pkg/mod.py", ".") == {"pkg"}
    assert PRDependencyGraphService._normalize_import("pkg/a/mod.py", "..") == {"pkg"}
    assert PRDependencyGraphService._normalize_import("src/app.ts", "./") == {"src"}


def test_resolve_import_to_changed_file_uses_prefix_match():
    path_aliases = {
        "src/components/Button.tsx": PRDependencyGraphService._build_file_aliases(
            "src/components/Button.tsx"
        )
    }

    assert PRDependencyGraphService._resolve_import_to_changed_file(
        "src/pages/Home.tsx",
        "components",
        path_aliases,
    ) == "src/components/Button.tsx"


def test_dependency_graph_mode_dynamic_config_registered():
    assert "pr_dependency_graph_mode" in DYNAMIC_CONFIG_GROUPS["pr_dependency_graph"]["keys"]
    assert DYNAMIC_CONFIG_LABELS["pr_dependency_graph_mode"] == "PR 依赖图模式"
    assert DYNAMIC_CONFIG_SELECT_OPTIONS["pr_dependency_graph_mode"] == [
        {"value": "ai", "label": "AI 生成（使用 LLM 分析）"},
        {"value": "static", "label": "静态分析（正则提取 import）"},
    ]
