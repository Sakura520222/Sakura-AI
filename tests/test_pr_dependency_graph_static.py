from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.config import (
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
)
from backend.services.ai_reviewer.pr_dependency_graph import PRDependencyGraphService
from backend.services.pr_analyzer import PRAnalysis, PRFileInfo


@pytest.fixture
def service():
    return PRDependencyGraphService(api_client=AsyncMock(), model="test-model")


def make_file(
    path: str, status: str = "modified", changes: int = 1
) -> PRFileInfo:
    return PRFileInfo(
        path=path,
        status=status,
        additions=changes,
        deletions=0,
        changes=changes,
        is_code_file=True,
    )


def make_analysis(
    code_files: list[PRFileInfo], *, is_incremental: bool = False
) -> PRAnalysis:
    return PRAnalysis(
        pr_id=1,
        pr_number=2,
        repo_full_name="owner/repo",
        total_files=len(code_files),
        total_additions=sum(file.additions for file in code_files),
        total_deletions=sum(file.deletions for file in code_files),
        total_changes=sum(file.changes for file in code_files),
        code_files=code_files,
        code_file_count=len(code_files),
        code_changes=sum(file.changes for file in code_files),
        strategy="small",
        should_skip=False,
        is_incremental=is_incremental,
    )


def make_github_file(path: str, status: str = "modified", changes: int = 1):
    return SimpleNamespace(
        filename=path,
        status=status,
        additions=changes,
        deletions=0,
        changes=changes,
    )


def test_incremental_graph_uses_all_pr_file_metadata(service):
    analysis = make_analysis([make_file("src/new.py")], is_incremental=True)
    pr = MagicMock()
    pr.changed_files = 3
    pr.get_files.return_value = [
        make_github_file("src/old.py"),
        make_github_file("src/new.py"),
        make_github_file("README.md"),
    ]
    strategy_config = MagicMock()
    strategy_config.should_skip_file.return_value = False
    strategy_config.is_code_file.side_effect = lambda path: path.endswith(".py")

    with patch(
        "backend.services.ai_reviewer.pr_dependency_graph.get_strategy_config",
        return_value=strategy_config,
    ):
        graph_files, total_file_count = service._get_graph_files_sync(analysis, pr)

    assert [file.path for file in graph_files] == ["src/old.py", "src/new.py"]
    assert total_file_count == 3
    pr.get_files.assert_called_once_with()


def test_incremental_graph_only_fetches_current_change_contents(service):
    current_file = make_file("src/new.py")
    analysis = make_analysis([current_file], is_incremental=True)
    graph_files = [make_file("src/old.py"), current_file]

    content_files = service._select_content_files(analysis, graph_files)

    assert content_files == [current_file]


def test_trim_files_prioritizes_current_incremental_changes(service):
    current_file = make_file("src/new.py", changes=1)
    graph_files = [
        make_file("src/old-large.py", changes=100),
        make_file("src/old-medium.py", changes=50),
        current_file,
    ]
    settings = SimpleNamespace(pr_dependency_graph_max_files=2)

    selected = service._trim_files(
        graph_files,
        settings,
        priority_paths={current_file.path},
    )

    assert [file.path for file in selected] == ["src/new.py", "src/old-large.py"]


def test_trim_files_excludes_removed_and_deleted_files(service):
    # GitHub File.status 对删除文件返回 "removed"，而非字面量 "deleted"，
    # 两种取值都应被裁剪逻辑排除。
    graph_files = [
        make_file("src/added.py", status="added", changes=200),
        make_file("src/removed.py", status="removed", changes=999),
        make_file("src/deleted.py", status="deleted", changes=888),
        make_file("src/modified.py", status="modified", changes=1),
    ]
    settings = SimpleNamespace(pr_dependency_graph_max_files=50)

    selected = service._trim_files(graph_files, settings)

    assert [file.path for file in selected] == ["src/added.py", "src/modified.py"]


def test_select_content_files_excludes_removed_files(service):
    current_file = make_file("src/new.py")
    deleted_file = make_file("src/gone.py", status="removed")
    analysis = make_analysis(
        [current_file, deleted_file], is_incremental=True
    )
    graph_files = [current_file, deleted_file]

    content_files = service._select_content_files(analysis, graph_files)

    assert content_files == [current_file]


def test_build_prompts_uses_cumulative_file_counts(service):
    settings = SimpleNamespace(pr_dependency_graph_max_nodes=25)
    strategy_config = MagicMock()
    strategy_config.config = {
        "pr_dependency_graph": {
            "user_template": (
                "{file_count}/{code_file_count}/{analyzed_file_count}\n"
                "{import_context}"
            )
        }
    }

    with patch(
        "backend.services.ai_reviewer.pr_dependency_graph.get_strategy_config",
        return_value=strategy_config,
    ):
        _, user_message = service._build_prompts(
            "imports",
            {"title": "Incremental graph"},
            settings,
            file_count=8,
            code_file_count=6,
            analyzed_file_count=4,
        )

    assert user_message == "8/6/4\nimports"


@pytest.mark.asyncio
async def test_generate_incremental_graph_wires_full_metadata_to_prompt(service):
    current_file = make_file("src/new.py")
    historical_file = make_file("src/old.py")
    analysis = make_analysis([current_file], is_incremental=True)
    settings = SimpleNamespace(
        pr_dependency_graph_max_files=50,
        pr_dependency_graph_max_nodes=25,
    )
    service._get_graph_files_sync = MagicMock(
        return_value=([historical_file, current_file], 3)
    )
    service._fetch_file_contents_sync = MagicMock(
        return_value={"src/new.py": "from src.old import helper\n"}
    )
    service._get_graph_mode = AsyncMock(return_value="ai")
    service._build_import_context = MagicMock(return_value="full import context")
    service._build_prompts = MagicMock(return_value=("system", "user"))
    service._validate_mermaid = MagicMock(return_value="graph TD\nN1 --> N2")
    service.update_pr_body_with_graph = AsyncMock()
    service.api_client.call_with_retry = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="graph TD\nN1 --> N2")
                )
            ]
        )
    )
    pr = MagicMock()

    with patch(
        "backend.services.ai_reviewer.pr_dependency_graph.get_settings",
        return_value=settings,
    ):
        graph = await service.generate_dependency_graph(
            analysis,
            {"title": "Incremental graph", "body": ""},
            pr,
        )

    assert graph == "graph TD\nN1 --> N2"
    fetched_files = service._fetch_file_contents_sync.call_args.args[0]
    assert fetched_files == [current_file]
    context_files = service._build_import_context.call_args.args[0]
    assert context_files == [historical_file, current_file]
    assert service._build_prompts.call_args.kwargs == {
        "file_count": 3,
        "code_file_count": 2,
        "analyzed_file_count": 2,
    }


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
            "package com.example;\nimport com.example.service.UserService;\n"
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


def test_static_incremental_merges_previous_graph_edges(service):
    # 增量审查时，历史节点与历史边来自 previous_graph，本轮变更文件提供新内容。
    code_files = [make_file("src/new.py")]
    file_contents = {"src/new.py": "from src.existing import X\n"}
    previous_graph = (
        'graph TD\n    N1["src/existing.py"]\n    N2["src/other.py"]\n'
        '    N1 --> N2\n'
    )

    graph = service._generate_static_mermaid(
        code_files,
        file_contents,
        max_nodes=25,
        previous_graph=previous_graph,
    )

    # 历史节点与新节点都应保留
    assert "src/existing.py" in graph
    assert "src/other.py" in graph
    assert "src/new.py" in graph
    # 历史边 (existing -> other) 必须保留；new -> existing 不会产生，因为
    # import 仅解析到本轮可用文件，历史节点不参与 import 解析。
    assert graph.count("-->") >= 1


def test_static_incremental_truncation_prioritizes_connected_nodes(service):
    # max_nodes 紧张时复用 _select_static_graph_nodes 的「连通优先」策略：
    # 有依赖边的节点（含历史边端点）优先保留，本轮变更文件若未解析到依赖边
    # 会被挤出。默认 max_nodes=25 通常不触发，此用例固化边界行为。
    code_files = [make_file("src/new.py")]
    file_contents = {"src/new.py": "from src.existing import X\n"}
    previous_graph = (
        "graph TD\n"
        '    N1["src/existing.py"]\n    N2["src/other.py"]\n'
        '    N3["src/another.py"]\n'
        "    N1 --> N2\n    N2 --> N3\n"
    )

    graph = service._generate_static_mermaid(
        code_files,
        file_contents,
        max_nodes=2,
        previous_graph=previous_graph,
    )

    # 两个连通历史节点保留，第三个历史节点与无边的本轮文件被截断
    assert "src/existing.py" in graph
    assert "src/other.py" in graph
    assert "src/another.py" not in graph
    assert "src/new.py" not in graph
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
    contents = {
        'src/components/<Widget>{v1}|(#%)".tsx': "export const widget = null;\n"
    }

    graph = service._generate_static_mermaid(files, contents, max_nodes=25)

    assert "&lt;Widget&gt;" in graph
    assert "&#123;v1&#125;&#124;&#40;&#35;&#37;&#41;'" in graph
    assert '".tsx' not in graph


def test_unescape_mermaid_label_reverses_escape():
    # previous_graph 节点 label 经过 _escape_mermaid_label 转义，
    # 解析时需要反向还原为原始路径。
    escaped = "src&#91;v1&#93;/pkg&#123;x&#125;.py"
    assert (
        PRDependencyGraphService._unescape_mermaid_label(escaped)
        == "src[v1]/pkg{x}.py"
    )


def test_parse_previous_graph_returns_empty_for_blank_input():
    for blank in ("", "   \n\t"):
        nodes, edges = PRDependencyGraphService._parse_previous_graph(blank)
        assert nodes == {}
        assert edges == set()


def test_parse_previous_graph_nodes_without_edges():
    previous_graph = 'graph TD\n    N1["src/a.py"]\n    N2["src/b.py"]\n'

    nodes, edges = PRDependencyGraphService._parse_previous_graph(previous_graph)

    assert nodes == {"N1": "src/a.py", "N2": "src/b.py"}
    assert edges == set()


def test_parse_previous_graph_omitted_marker_does_not_pollute_edges():
    # 静态生成器裁剪时写入的 OMITTED 展示节点不参与依赖边；解析后真实边应完整
    # 保留，OMITTED 不应出现在任何边端点。
    previous_graph = (
        "graph TD\n"
        '    N1["src/a.py"]\n    N2["src/b.py"]\n'
        '    N1 --> N2\n'
        '    OMITTED["... 3 more dependencies omitted"]\n'
    )

    _, edges = PRDependencyGraphService._parse_previous_graph(previous_graph)

    assert edges == {("src/a.py", "src/b.py")}


def test_parse_previous_graph_unescapes_label_entities():
    # 节点 label 中的 HTML 实体（_escape_mermaid_label 产物）应被还原为原始路径。
    previous_graph = 'graph TD\n    N1["src&#91;v1&#93;/pkg&#123;x&#125;.py"]\n'

    nodes, _ = PRDependencyGraphService._parse_previous_graph(previous_graph)

    assert nodes == {"N1": "src[v1]/pkg{x}.py"}


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

    assert (
        PRDependencyGraphService._resolve_import_to_changed_file(
            "src/pages/Home.tsx",
            "components",
            path_aliases,
        )
        == "src/components/Button.tsx"
    )


def test_dependency_graph_mode_dynamic_config_registered():
    assert (
        "pr_dependency_graph_mode"
        in DYNAMIC_CONFIG_GROUPS["pr_dependency_graph"]["keys"]
    )
    assert DYNAMIC_CONFIG_LABELS["pr_dependency_graph_mode"] == "PR 依赖图模式"
    assert DYNAMIC_CONFIG_SELECT_OPTIONS["pr_dependency_graph_mode"] == [
        {"value": "ai", "label": "AI 生成（使用 LLM 分析）"},
        {"value": "static", "label": "静态分析（正则提取 import）"},
    ]
