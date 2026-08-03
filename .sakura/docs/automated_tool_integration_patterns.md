# 自动化工具集成模式

> 来源：PR442、PR446、PR453、PR455 审查反思，2026年7-8月

## 一、Dependabot与Gitflow冲突模式

### 问题
Dependabot默认分支命名`dependabot/<category>/<target>/<dep>`与项目Gitflow分支保护规则`^(feature|fix|...)\/.+`产生结构性冲突，导致CI失败、PR无法合并。**这是反复出现的模式**。

### 解决方案
1. 在`.github/dependabot.yml`中配置`target-branch`指向develop分支
2. 在仓库设置中为`dependabot/*`前缀添加分支保护规则例外
3. 记录决策为团队共识，写入项目文档

## 二、自动化PR描述一致性

### 问题
Dependabot PR描述可能与最终合并后的diff不一致——描述仅覆盖初始提交，后续合并的依赖升级未被更新到描述中（PR446）。

### 审查规则
1. 核对最终`requirements.txt`差异与PR描述是否完全匹配
2. 不匹配时要求修正或拆分PR
3. 描述覆盖度应以最终合并后的主diff为准

## 三、依赖升级PR审查清单

对于Dependabot/Renovate生成的PR，审查清单强制包含：
1. **分支合规性**：源分支和目标分支是否符合仓库分支策略
2. **版本跳跃幅度**：minor/major/patch，是否涉及Breaking Changes
3. **Changelog核对**：对照Release Notes评估对项目的影响
4. **项目调用点验证**：grep确认项目实际使用情况
5. **CI状态**：构建是否通过，失败原因是否与本次变更相关

## 四、人机协作缝隙修补

自动化工具高效产出，但下游人工规则未及时适配时，会产生"合规性幻觉"。审查者的核心价值：
- 识别自动化行为与项目独特规范之间的错配
- 将发现转化为对项目配置的改进（而非仅标记问题）
- 在approve前要求可执行的验证行动，而非仅"建议确认"
