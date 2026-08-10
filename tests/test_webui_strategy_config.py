"""WebUI 审查策略配置保存的回归测试。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
import yaml

from backend.webui.routes import config as config_routes


class _FormRequest:
    """只实现策略保存路由需要的 Request 接口。"""

    def __init__(self, form_data: Mapping[str, str]) -> None:
        self._form_data = form_data

    async def form(self) -> Mapping[str, str]:
        return self._form_data


def _strategies_config() -> dict[str, Any]:
    return {
        "strategies": {
            key: {
                "name": key,
                "conditions": {"max_files": 1, "max_lines": 1},
                "prompt": "Review the change.",
            }
            for key in config_routes.STRATEGY_KEYS
        }
    }


def _strategy_form() -> dict[str, str]:
    form = {}
    for key in config_routes.STRATEGY_KEYS:
        form.update(
            {
                f"strategy_{key}_name": key,
                f"strategy_{key}_max_files": "1",
                f"strategy_{key}_max_lines": "1",
                f"strategy_{key}_prompt": "Review the change.",
            }
        )
    form["strategy_large_max_files"] = "999999"
    form["strategy_large_max_lines"] = "99999999"
    return form


@pytest.mark.asyncio
async def test_save_strategies_accepts_unbounded_large_conditions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    strategy_path = tmp_path / "strategies.yaml"
    strategy_path.write_text(
        yaml.safe_dump(_strategies_config(), allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(config_routes, "STRATEGIES_PATH", strategy_path)
    monkeypatch.setattr(config_routes, "reload_strategy_config", lambda: None)
    monkeypatch.setattr(config_routes, "detect_language", lambda: "zh-CN")

    async def fake_log_admin_action(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_toast_redirect(*args: object, **kwargs: object) -> dict[str, object]:
        return {"args": args, "kwargs": kwargs}

    monkeypatch.setattr(config_routes, "log_admin_action", fake_log_admin_action)
    monkeypatch.setattr(config_routes, "toast_redirect", fake_toast_redirect)

    response = await config_routes.save_strategies_section(
        _FormRequest(_strategy_form()),
        db=object(),
        user={"sub": "admin", "user_id": 1},
        csrf_token="csrf-token",
        section="strategies",
    )

    assert response["args"][1] == "toast.strategy_saved"
    saved_config = yaml.safe_load(strategy_path.read_text(encoding="utf-8"))
    assert saved_config["strategies"]["large"]["conditions"] == {
        "max_files": 999999,
        "max_lines": 99999999,
    }
