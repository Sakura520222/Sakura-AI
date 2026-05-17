"""Canonical search exclude patterns for file traversal tools.

Shared by grep_tool.py and glob_tool.py to avoid duplication.
"""

# Directories to exclude from search/glob (VCS + package managers + build artifacts)
SEARCH_EXCLUDES: frozenset[str] = frozenset({
    ".git", ".svn", ".hg", ".bzr",
    "__pycache__",
    ".venv", "venv", "env",
    "node_modules", "vendor", "bower_components",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs", "egg-info",
    ".cargo", "target",
    "chroma_data",
})
