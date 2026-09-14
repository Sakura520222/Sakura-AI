# 依赖管理体系架构与决策

> 来源：PR573-PR577 反思；补充 architecture_decisions.md 未覆盖的依赖管理部分。

## 1. 依赖声明结构
- **根项目**：`pyproject.toml`（权威声明）+ `requirements.txt`（1:1 镜像）+ `uv.lock`（发布产物）。
- **子项目**：`sandboxer/`、`updater/` 各自独立 pyproject.toml，与根项目共享关键依赖但版本下限需人工保持一致——这是版本漂移的架构性根源。
- **构建链现状**：CI 走 `pip install -r requirements.txt`，发布打包携带 uv.lock。双轨解析是已知缺陷，目标态为全链路 `uv sync --locked`。

## 2. 关键设计决策
1. **锁文件入库并作为发布依据**：保证可复现构建；代价是每次依赖变更必须同步锁文件（CI 以 `uv lock --check` 保障）。
2. **双清单镜像 + 自动比对测试**：兼容 pip/uv 两种部署方式；用单元测试锁定镜像关系，防止手动同步出错。
3. **单点消费封装**：watchfiles 仅被 `backend/core/hot_reload.py` 引用，升级零业务代码改动。项目鼓励"外部库使用集中化"以降低升级评估成本。
4. **死依赖处置**：alembic 声明但零导入点（建表走 `Base.metadata.create_all()`，迁移由外部工具执行）。决议：注明保留意图或移除；删除前审计 CI/脚本/镜像等非代码消费点。
5. **Python 3.14 目标运行时**：`.python-version` 已指向 3.14；依赖升级需确认 cp314 wheel 存在，用 `pip download --only-binary` 预检。

## 3. 版本漂移风险面（三镜像）
- Web / sandboxd / Agent Runner 三镜像独立发布，历史上出现半更新状态（ISSUE570）：Updater 误报"更新成功"但能力不匹配。
- 对策：`/v1/status` 暴露 `capabilities` 能力协商并 fail-closed 检查；发布时生成一次性 manifest，部署组件读取同一 manifest；版本漂移自动检测 + 幂等回滚；`start.sh` 标签一致性断言（dev-channel 只能拉对应 web 镜像）。

## 4. venv 双向隔离
- `.venv/local` 与 `.venv/sandbox` 分离，防止依赖泄漏；两端版本锁定须同步，容器退出时清理 sandbox venv（post-shutdown hook）。

## 5. 与其他体系的关系
- 配置中心 `backend/core/config.py` 承担网络策略/SMTP/Telegram 等配置；策略互斥（offline vs full_access）需在配置层校验。
- `backend/__init__.py` 的 env 注入（PTB_TIMEDELTA）属全局副作用：包导入即生效、加载顺序敏感；业务代码禁止散布 `os.environ.setdefault`，统一走配置模块。
