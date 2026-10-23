"""uv 锁文件自动同步工作流契约 / uv lockfile auto-sync workflow contracts.

锁定 uv-lock-sync.yml 以 pull_request_target 运行所需的安全属性,防止无意识削弱:
- 触发器必须是 pull_request_target:Dependabot 触发的 pull_request 自 2021-03
  起 被 GitHub 视同 fork 运行,读不到仓库 secrets(含 MY_RELEASE_PAT),
  锁文件推送将永远走降级路径;
- 作业门禁必须同时限定同仓库与 Dependabot 身份,并兼容 dependabot[bot]
  (REST/webhook 载荷)与 app/dependabot(GraphQL)两种登录形态;
- 必须显式检出 PR head SHA(pull_request_target 默认检出基分支),
  检出凭据用 MY_RELEASE_PAT,GITHUB_TOKEN 的推送不会触发 PR 的 CI 重跑。

Locks in the safety properties that the uv lockfile sync workflow must keep
while running under pull_request_target: a secret-visible trigger, a tight
same-repo Dependabot gate covering both login representations, an explicit
head SHA checkout, and PAT-authenticated pushes only.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "uv-lock-sync.yml"


def _workflow() -> dict:
    """解析工作流 YAML;PyYAML 按 YAML 1.1 把键 `on` 解析为布尔 True。"""
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    return _workflow()["jobs"]["sync-lock"]


def test_trigger_uses_pull_request_target() -> None:
    """触发器必须是 pull_request_target,不得回退 pull_request。"""
    triggers = _workflow()[True]
    assert "pull_request_target" in triggers
    assert "pull_request" not in triggers
    assert "requirements.txt" in triggers["pull_request_target"]["paths"]


def test_gate_restricts_to_same_repo_dependabot() -> None:
    """门禁必须同时限定同仓库与 Dependabot 双登录形态。"""
    condition = _job()["if"]
    assert "head.repo.full_name == github.repository" in condition
    assert "dependabot[bot]" in condition
    assert "app/dependabot" in condition


def test_checkout_pins_pr_head_sha_with_pat() -> None:
    """检出必须显式指向 head SHA 并以 PAT 认证,而非基分支默认值。"""
    checkout = _job()["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert checkout["with"]["token"] == "${{ secrets.MY_RELEASE_PAT }}"


def test_push_uses_origin_without_token_in_url() -> None:
    """推送必须走 origin 凭据,不得把 PAT 拼进 URL(对齐 gitflow-sync 惯例)。"""
    push = next(s for s in _job()["steps"] if s.get("name") == "提交并推送锁文件变更")
    script = push["run"]
    assert 'git push origin "HEAD:refs/heads/${HEAD_BRANCH}"' in script
    assert "x-access-token" not in script
