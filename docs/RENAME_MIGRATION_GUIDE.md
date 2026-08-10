# Sakura AI 重命名迁移指南

> 项目从 Sakura-AI-Reviewer 重命名为 Sakura AI 的运维步骤。改名已完成，本文档面向从旧版升级的维护者保留作历史参考。

← [文档索引](README.md) · [README](../README.md)

---

> 改名已完成。如果你是新部署，无需阅读本文档；仅当从旧版 `Sakura-AI-Reviewer` 升级时参考。本版本不提供自动数据库迁移脚本。

## 1. 改名内容总览

| 上下文 | 旧 | 新 |
|---|---|---|
| 显示名 | Sakura AI Reviewer | Sakura AI |
| Docker 镜像/容器(web) | sakura-ai-reviewer | sakura-ai |
| MySQL 库名 | sakura-pr | sakura_ai |
| GitHub 主仓库 | Sakura520222/Sakura-AI-Reviewer | Sakura520222/Sakura-AI |
| GitHub APP 仓库 | Sakura520222/Sakura-AI-Reviewer-APP | Sakura520222/Sakura-AI-APP |
| /health service 字段 | Sakura AI Reviewer | sakura-ai |
| 评论文案 | 此评论由 Sakura AI Reviewer 自动生成 | 此评论由 Sakura AI 自动生成。 |

**保留不变**(功能品牌,未随项目改名):
- 配置键:`sakura_memory_*`、`sakura_reflection_*`、`sakura_consolidation_*`、`sakura_extraction_*`(约 40 个)
- 数据库表:`sakura_memory_states`
- 目录:`.sakura/`
- 分支前缀:`sakura-agent/`、`sakura-memory/`
- 环境变量:`SAKURA_ENV`、`SAKURA_DEV_BOOTSTRAP`、`SAKURA_SKIP_BACKGROUND_TASKS`、`SAKURA_CONNECTION_CONFIG_PATH`
- 功能标识:`sakura_pr_review`
- 文件:`sakura_memory_service.py`、`sakura_memory.html`

## 2. GitHub 仓库改名步骤

1. 主仓库:GitHub Settings → Repository name → `Sakura-AI` → Rename。旧 URL 自动重定向到新名。
2. APP 仓库:同上,改为 `Sakura-AI-APP`。

## 3. 本地 git remote 更新

```bash
git remote set-url origin https://github.com/Sakura520222/Sakura-AI.git
git remote -v  # 核对 fetch/push 均为新地址
```

## 4. 全新部署使用 sakura_ai 数据库

`docker/mysql-init/init.sql` 已建 `sakura_ai` 库。Setup Wizard 填写的 `DATABASE_URL` 示例:

```
mysql+aiomysql://user:pass@localhost:3306/sakura_ai
```

## 5. 已部署实例的数据保留选择

本版本不提供自动迁移脚本。存量实例由管理员选择:

- **保留旧库 `sakura-pr`**:`config/connection.json` 的 `database_url` 继续指向 `sakura-pr`,应用照常运行(与新 `init.sql` 不一致,仅作过渡,不推荐长期)。
- **手动迁移**:`mysqldump` 导出 `sakura-pr`,导入到新建的 `sakura_ai`,更新 `config/connection.json` 指向 `sakura_ai`。
- **放弃旧数据**:全新部署 `sakura_ai`(仅适合无重要数据的环境)。

手动迁移示例(Linux/bash):

```bash
mysqldump -u root -p sakura-pr > /tmp/sakura-pr.sql
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS sakura_ai CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -u root -p sakura_ai < /tmp/sakura-pr.sql
```

迁移后编辑 `config/connection.json`,把 `database_url` 的库名改为 `sakura_ai`,重启应用。

## 6. Docker 镜像和容器清理

旧镜像 `sakura-ai-reviewer` 不再使用:

```bash
docker compose -f docker/docker-compose.yml down
docker rmi sakura-ai-reviewer  # 可选,释放磁盘
```

重新部署(会构建/使用新镜像 `sakura-ai`):

```bash
./start.sh --rebuild
# 或
docker compose -f docker/docker-compose.yml up -d --build
```

## 7. GitHub App 检查与可选迁移

**优先尝试修改现有 GitHub App**:在 GitHub App 设置中将应用名称改为「Sakura AI」,并确认:
- slug 与 Bot 用户名
- Webhook URL
- 安装状态(已安装到目标仓库/组织)
- 回调配置

确认目标 slug `sakura-ai` 可用且名称无冲突后,在 Setup Wizard 重新测试 GitHub App 凭证,`bot_username` 会从新 App 信息自动拉取。

**新建 App 仅作兜底**:仅当目标 slug `sakura-ai` 无法获得、名称冲突,或现有 App 设置无法达到预期时,才新建 GitHub App。届时需迁移:
- App ID、Client ID
- 私钥(重新生成)
- Webhook Secret
- 重新安装到仓库/组织
- 清理旧 App 安装
- 通过 Setup Wizard 全量替换 GitHub 相关配置
- 重新跑 Setup Wizard 让 `bot_username` 从新 App 拉取

## 8. 验证清单

部署/迁移完成后逐项确认:

- `/openapi.json` 的 `info.title` = `Sakura AI`
- `/health` 返回 `service` = `sakura-ai`(注意:若外部监控断言旧值 `Sakura AI Reviewer`,需同步更新监控规则)
- WebUI 首页/关于页显示 `Sakura AI`
- `docker compose -f docker/docker-compose.yml config` 显示 web image/container = `sakura-ai`
- MySQL 库名 = `sakura_ai`
- 残留搜索(PowerShell,Windows 环境)零命中:

```powershell
$files = git ls-files |
  Where-Object {
    $_ -notmatch '^\.sakura/' -and
    $_ -notmatch '^\.understand-anything/' -and
    $_ -ne 'docs/RENAME_MIGRATION_GUIDE.md'
  }

Select-String -Path $files -Pattern 'Sakura AI Reviewer','Sakura-AI-Reviewer','sakura-ai-reviewer','sakura-pr','pr-reviewer-worker','pr_reviewer'
```

预期零命中(仅扫描 git 跟踪文件,自动避开本地 `.env`、日志、缓存、虚拟环境等未跟踪/忽略文件;额外排除 `.sakura/`、`.understand-anything/` 历史快照以及本迁移指南自身)。

## 9. 回滚步骤

- **代码**:`git revert` 对应的重命名 commit/PR。
- **GitHub 仓库**:Settings 改回旧名(旧 URL 重定向仍可用)。
- **数据库**:若已迁移到 `sakura_ai`,可从迁移前的 `mysqldump` 备份恢复 `sakura-pr`。
- **Docker**:`docker rmi sakura-ai`,保留或重建 `sakura-ai-reviewer`。
- **GitHub App**:若新建了 App,切回旧 App 凭证(保留旧 App 安装可快速回退)。

---

*最后更新：2026-8-10 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
