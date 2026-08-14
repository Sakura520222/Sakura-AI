"""Sakura AI 品牌与审查评论落款。

本模块必须保持零 backend 依赖：`backend.core.github_app` 等纯 GitHub
路径会在进程启动时导入它，任何传递依赖（尤其是 ai_reviewer 包，其
`__init__` 会急切加载完整 AI 审查器栈）都会让轻量路径白白付出启动
时间和内存成本。/ Keep this module dependency-free: lightweight
GitHub-only paths import it at process start, so any transitive import
(notably the ai_reviewer package, whose `__init__` eagerly loads the full
AI reviewer stack) would silently cost them startup time and memory.
"""

# 所有发布到 GitHub 的 AI 文案（审查评论、Agent PR 描述、扫描报告）中
# 品牌词与落款的中英文唯一来源 / Canonical repo URL and zh/en wording
# of the signature/branding every AI-generated GitHub text carries.
SAKURA_AI_REPO_URL = "https://github.com/Sakura520222/Sakura-AI"


def review_signature_footer(is_english: bool) -> str:
    """构建审查评论落款文本（不含分隔线）/ Build the signature line."""
    if is_english:
        return (
            f"*This comment was generated automatically "
            f"by [Sakura AI]({SAKURA_AI_REPO_URL}).*"
        )
    return f"*此评论由 [Sakura AI]({SAKURA_AI_REPO_URL}) 自动生成。*"


def append_review_signature(body: str, is_english: bool) -> str:
    """幂等地为评论正文追加落款（正文已含落款时原样返回）。

    重试或降级重发同一评论时可能重复经过此函数，落款不能叠加。
    """
    footer = review_signature_footer(is_english)
    if footer in body:
        return body
    return f"{body.rstrip()}\n\n---\n\n{footer}"
