"""标签推荐模块

从原 ai_reviewer.py 迁移的标签推荐相关方法：
- recommend_labels (1837-1940行)
"""

from typing import Any

from loguru import logger

from backend.services.ai_reviewer.constants import LABEL_RECOMMENDATION_TEMPERATURE


class LabelRecommender:
    """标签推荐器

    负责根据 PR 的代码变更推荐合适的标签。
    """

    def __init__(
        self, api_client, prompt_builder, result_parser, model: str | None = None
    ):
        """初始化标签推荐器

        Args:
            api_client: AI API 客户端
            prompt_builder: 提示词构建器
            result_parser: 结果解析器
            model: 保留的兼容参数；实际模型由 summary 角色绑定解析
        """
        self.api_client = api_client
        self.prompt_builder = prompt_builder
        self.result_parser = result_parser
        # 模型由 summary 角色绑定解析；保留属性仅兼容旧的 fake client。
        self.model = model or ""

    async def recommend_labels(
        self,
        context: dict[str, Any],
        available_labels: dict[str, dict[str, Any]],
        pr_info: dict[str, Any],
        existing_labels: list[str] | None = None,
        *,
        invocation_context: Any = None,
        observer: Any = None,
        propagate_errors: bool = False,
    ) -> list[dict[str, Any]]:
        """推荐PR标签

        Args:
            context: 审查上下文
            available_labels: 可用的标签字典
            pr_info: PR信息（包含标题、描述等）
            existing_labels: PR 已有的标签名称列表（用于增量审查时避免冲突）

        Returns:
            推荐标签列表，格式：[{"name": str, "confidence": float, "reason": str}]
        """
        try:
            logger.info("开始AI标签推荐分析")

            # 判断是否为增量审查
            analysis = context.get("analysis")
            is_incremental = bool(
                analysis and getattr(analysis, "is_incremental", False)
            )

            # 构建系统提示词（传入增量审查状态和已有标签）
            system_prompt = self._build_system_prompt(
                is_incremental=is_incremental,
                existing_labels=existing_labels,
            )

            # 构建用户消息
            user_message = self.prompt_builder.build_label_recommendation_message(
                context, available_labels, pr_info
            )

            response = await self.api_client.call_with_retry(
                model="",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=LABEL_RECOMMENDATION_TEMPERATURE,
                role="summary",
                context=invocation_context,
                observer=observer,
            )

            # 提取响应
            recommendation_text = response.choices[0].message.content

            logger.debug(f"AI标签推荐完整响应:\n{recommendation_text}")
            logger.info(f"AI标签推荐响应长度: {len(recommendation_text)} 字符")

            # 解析推荐结果
            recommendations = self.result_parser.parse_label_recommendation(
                recommendation_text
            )

            logger.info(f"AI标签推荐完成，共 {len(recommendations)} 个推荐")
            return recommendations

        except Exception as e:
            logger.error(f"AI标签推荐失败: {e}", exc_info=True)
            if propagate_errors:
                raise
            return []

    def _build_system_prompt(
        self,
        is_incremental: bool = False,
        existing_labels: list | None = None,
    ) -> str:
        """构建标签推荐系统提示词

        Args:
            is_incremental: 是否为增量审查
            existing_labels: PR 已有的标签名称列表

        Returns:
            系统提示词
        """
        base_prompt = """你是一个专业的代码审查助手，擅长根据代码变更的内容和性质为Pull Request推荐合适的标签。

## 标签推荐原则

1. **准确性**: 仔细分析代码变更的实际内容，不要仅凭文件名或路径判断
2. **多维度**: 可以同时推荐多个标签，覆盖不同维度
3. **置信度**: 为每个标签给出0-1之间的置信度分数
   - 0.8-1.0: 非常确定，明显符合该标签特征
   - 0.6-0.8: 较为确定，很可能符合
   - 0.4-0.6: 可能符合，需要更多信息确认
   - 0.2-0.4: 有一定可能，但不确定
   - 0.0-0.2: 仅作建议参考
4. **理由说明**: 为每个推荐标签提供简洁的理由

## 标签类型参考

- **bug**: 修复错误、缺陷、边界条件问题
- **enhancement**: 新功能、功能增强、新增API
- **refactor**: 代码重构、结构优化（非功能性变更）
- **performance**: 性能优化、缓存改进、算法优化
- **documentation**: 文档更新、README、注释
- **test**: 测试代码、测试用例、测试修复
- **dependencies**: 依赖更新、包管理
- **ci**: CI/CD配置、工作流、自动化
- **style**: 代码风格、格式化、linting
- **build**: 构建配置、编译脚本

## 输出格式

请以JSON格式返回推荐结果：

```json
{
  "labels": [
    {
      "name": "标签名称",
      "confidence": 0.85,
      "reason": "推荐理由说明"
    }
  ]
}
```

**重要输出要求**：
- 请仅输出 JSON 格式结果，不要包含任何解释文字或 Markdown 标记
- 确保以 '{' 开头，以 '}' 结尾
- 不要添加 ```json 或 ``` 等标记
- 只推荐列表中存在的标签
- 最多推荐3-5个标签
- 置信度必须是0-1之间的数字
- 理由说明要简洁具体
"""

        # 增量审查时的额外指导
        if is_incremental:
            base_prompt += """

## ⚠️ 增量审查标签推荐策略

当前是对 PR 的**增量审查**（即 PR 已有之前的审查记录，本次审查的是新增提交）。

### 核心原则：**整体意图优先**

1. **尊重 PR 整体意图**: 标签应反映整个 PR 的目的，而非仅看本次增量提交的内容
   - 如果 PR 整体是一个新功能（enhancement），即使本次增量提交中包含了对之前代码错误的修复，也不应添加 "bug" 标签
   - 如果 PR 整体是重构（refactor），即使本次增量提交修复了一个小问题，也不应添加 "bug" 标签
2. **已存在标签的考虑**: PR 已有的标签反映了 PR 的整体意图，推荐新标签时应与之保持一致
3. **避免语义冲突**: 不要推荐与已有标签语义矛盾的标签
   - 已有 "enhancement" → 不要推荐 "bug"（除非整个 PR 性质确为 bug 修复）
   - 已有 "refactor" → 不要推荐 "bug"（重构中的修复不应视为 bug 修复）
   - 已有 "bug" → 可以推荐 "enhancement"（如果 bug 修复同时引入了改进）
4. **补充而非替换**: 增量审查的标签推荐应侧重于**补充**遗漏的标签，而非改变 PR 的性质判定
"""

        # PR 已有标签提示
        if existing_labels:
            base_prompt += f"""

## PR 当前已有标签

该 PR 已被标记以下标签: {", ".join(f'"{lbl}"' for lbl in existing_labels)}

请在推荐时考虑这些已有标签所反映的 PR 整体意图，避免推荐语义矛盾的标签。
"""

        return base_prompt
