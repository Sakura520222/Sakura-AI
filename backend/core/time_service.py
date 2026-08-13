"""统一的时间、时区和 RFC3339 契约。

所有时间点在领域层都必须是带时区的 ``datetime``，并以 UTC 作为存储和
协议表示。应用时区只用于日历计算和用户显示；持续时间则使用注入的
monotonic 时钟，避免系统校时影响超时和 uptime。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic as _monotonic
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from tzlocal import get_localzone_name


class Clock(Protocol):
    """TimeService 可注入的时钟接口。"""

    def now_utc(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """生产环境时钟：OS wall clock + 单调时钟。"""

    @staticmethod
    def now_utc() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def monotonic() -> float:
        return _monotonic()


class InvalidTimezoneError(ValueError):
    """应用时区配置无效或系统时区无法解析。"""


class DateTimeLocalError(ValueError):
    """datetime-local 值落在 DST gap 或未消除 fold 歧义。"""


def _validate_zone_name(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name or name != name.strip():
        raise InvalidTimezoneError("时区必须是非空 IANA 名称")
    if name in {"CST", "EST", "PST", "MST", "CET", "EET", "IST"}:
        raise InvalidTimezoneError("不接受有歧义的时区缩写")
    # Explicitly reject fixed-offset syntax.  IANA Etc/GMT names remain valid
    # database zones because they are unambiguous named zones.
    if name.upper().startswith(("UTC+", "UTC-", "GMT+", "GMT-")):
        raise InvalidTimezoneError("不接受固定 offset 时区")
    if name != "UTC" and name not in available_timezones():
        raise InvalidTimezoneError(f"未知 IANA 时区: {name}")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise InvalidTimezoneError(f"未知 IANA 时区: {name}") from exc


def resolve_timezone(configured_timezone: str = "system") -> ZoneInfo:
    """解析并校验应用时区。

    ``system`` 每次调用都重新读取系统 IANA 名称；TimeService 只在启动时
    调用一次，因此进程运行期间不会因 OS 环境变化漂移。
    """

    if configured_timezone == "system":
        try:
            system_name = get_localzone_name()
        except Exception as exc:  # pragma: no cover - platform-specific details
            raise InvalidTimezoneError("无法解析系统 IANA 时区") from exc
        if not system_name:
            raise InvalidTimezoneError("无法解析系统 IANA 时区")
        configured_timezone = system_name
    return _validate_zone_name(configured_timezone)


def _require_aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间点必须是带有效 offset 的 aware datetime")
    return value


def format_rfc3339(value: datetime) -> str:
    """序列化为带微秒的 UTC ``Z`` RFC3339。"""

    instant = _require_aware(value).astimezone(UTC)
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime:
    """解析带 ``Z`` 或 numeric offset 的 RFC3339，并归一化为 UTC。"""

    if not isinstance(value, str):
        raise ValueError("RFC3339 时间必须是字符串")
    # RFC3339 requires a literal ``T`` separator, seconds, and either ``Z`` or
    # a numeric offset.  ``datetime.fromisoformat`` also accepts a space and
    # naive values, so validate the wire shape before delegating date math.
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ValueError("无效 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(
            f"{value[:-1]}+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise ValueError("无效 RFC3339 时间") from exc
    return _require_aware(parsed).astimezone(UTC)


def to_app_timezone(value: datetime, zone: ZoneInfo | str) -> datetime:
    """将 aware UTC/offset 时间点转换到应用时区。"""

    target = zone if isinstance(zone, ZoneInfo) else resolve_timezone(zone)
    return _require_aware(value).astimezone(target)


def format_display(value: datetime, zone: ZoneInfo | str, *, seconds: bool = True) -> str:
    """输出带 numeric offset 的用户可见时间，避免 DST 重复小时歧义。"""

    local = to_app_timezone(value, zone)
    pattern = "%Y-%m-%d %H:%M:%S%z" if seconds else "%Y-%m-%d %H:%M%z"
    return local.strftime(pattern)


def local_date(value: datetime, zone: ZoneInfo | str) -> date:
    return to_app_timezone(value, zone).date()


def start_of_local_day(day: date, zone: ZoneInfo | str) -> datetime:
    """返回应用日历日的第一个有效 UTC instant。

    少数 IANA 时区会在午夜向前跳时。表单中的不存在时间仍应被拒绝，
    但日历分桶边界必须落到跳时后的第一个有效 instant；若整个日期被
    跳过（例如国际日期变更线调整），则该日期是一个空区间，其起点与
    下一有效日期的起点相同。
    """

    zone_info = zone if isinstance(zone, ZoneInfo) else resolve_timezone(zone)
    midnight = datetime.combine(day, time.min)
    try:
        return parse_datetime_local(
            midnight.isoformat(timespec="minutes"),
            zone_info,
            fold=0,
        )
    except DateTimeLocalError:
        # Attaching both folds to an imaginary midnight and round-tripping via
        # UTC yields the adjacent real instants.  The earliest result whose
        # local date is not before ``day`` is the correct lower calendar bound.
        candidates = []
        for fold in (0, 1):
            candidate = (
                midnight.replace(tzinfo=zone_info, fold=fold)
                .astimezone(UTC)
                .astimezone(zone_info)
            )
            if candidate.date() >= day:
                candidates.append(candidate.astimezone(UTC))
        if candidates:
            return min(candidates)

        # Defensive fallback for unusual historical transitions whose two
        # imaginary-midnight projections both precede the requested date.
        return start_of_local_day(day + timedelta(days=1), zone_info)


def parse_local_date_boundary(
    value: str,
    zone: ZoneInfo | str,
    *,
    exclusive_end: bool = False,
) -> datetime:
    """Parse a YYYY-MM-DD filter in the application calendar as aware UTC."""

    day = datetime.strptime(value, "%Y-%m-%d").date()
    if exclusive_end:
        day += timedelta(days=1)
    return start_of_local_day(day, zone)


def parse_datetime_local(
    value: str,
    zone: ZoneInfo | str,
    *,
    fold: int | None = None,
) -> datetime:
    """解析应用时区的 ``datetime-local`` 并检测 DST gap/fold。

    重复小时要求调用方明确传入 ``fold=0``（较早 offset）或 ``fold=1``
    （较晚 offset）；不存在的小时始终返回字段级错误。
    """

    zone_info = zone if isinstance(zone, ZoneInfo) else resolve_timezone(zone)
    try:
        naive = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DateTimeLocalError("无效 datetime-local 值") from exc
    if naive.tzinfo is not None:
        raise DateTimeLocalError("datetime-local 不应包含 offset")

    candidates: list[datetime] = []
    for candidate_fold in (0, 1):
        candidate = naive.replace(tzinfo=zone_info, fold=candidate_fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone_info).replace(tzinfo=None)
        if round_trip == naive:
            candidates.append(candidate)
    unique_offsets = {candidate.utcoffset() for candidate in candidates}
    if not candidates:
        raise DateTimeLocalError("datetime-local 落在 DST gap（不存在的本地时间）")
    if len(unique_offsets) > 1:
        if fold not in (0, 1):
            raise DateTimeLocalError("datetime-local 落在 DST fold，必须选择 offset")
        selected = next(candidate for candidate in candidates if candidate.fold == fold)
        return selected.astimezone(UTC)
    if fold not in (None, 0, 1):
        raise DateTimeLocalError("fold 必须为 0 或 1")
    return candidates[0].astimezone(UTC)


def datetime_local_value(value: datetime, zone: ZoneInfo | str) -> str:
    """为编辑表单生成应用时区的无 offset 本地值。"""

    return to_app_timezone(value, zone).strftime("%Y-%m-%dT%H:%M")


def datetime_local_fold(value: datetime, zone: ZoneInfo | str) -> int:
    """Return the PEP 495 fold that preserves an instant in a local edit form."""

    return to_app_timezone(value, zone).fold


def filename_timestamp(value: datetime | None = None, zone: ZoneInfo | str | None = None) -> str:
    """Safe local-calendar timestamp for human-facing download filenames."""

    service = get_time_service() if value is None or zone is None else None
    instant = value or service.now_utc()  # type: ignore[union-attr]
    target = zone or service.zone  # type: ignore[union-attr]
    local = to_app_timezone(instant, target)  # type: ignore[arg-type]
    offset = local.strftime("%z") or "+0000"
    zone_name = str(getattr(target, "key", target)).replace("/", "-").replace(" ", "_")
    return f"{local:%Y%m%d-%H%M%S}-{offset}-{zone_name}"


@dataclass(frozen=True, slots=True)
class TimeService:
    """进程级冻结的时间上下文。"""

    configured_timezone: str = "system"
    clock: Clock | None = None

    def __post_init__(self) -> None:
        resolved = resolve_timezone(self.configured_timezone)
        object.__setattr__(self, "resolved_timezone", resolved.key)
        object.__setattr__(self, "zone", resolved)
        if self.clock is None:
            object.__setattr__(self, "clock", SystemClock())

    # assigned in __post_init__; kept out of the public constructor so callers
    # cannot accidentally provide a mutable ZoneInfo that changes at runtime.
    resolved_timezone: str = field(default="", init=False)
    zone: ZoneInfo | None = field(default=None, init=False)

    def now_utc(self) -> datetime:
        value = self.clock.now_utc()  # type: ignore[union-attr]
        return _require_aware(value).astimezone(UTC)

    def monotonic(self) -> float:
        return self.clock.monotonic()  # type: ignore[union-attr]

    def to_app_timezone(self, value: datetime) -> datetime:
        return to_app_timezone(value, self.zone)  # type: ignore[arg-type]

    def format_display(self, value: datetime, *, seconds: bool = True) -> str:
        return format_display(value, self.zone, seconds=seconds)  # type: ignore[arg-type]

    def parse_datetime_local(self, value: str, *, fold: int | None = None) -> datetime:
        return parse_datetime_local(value, self.zone, fold=fold)  # type: ignore[arg-type]


_time_service: TimeService | None = None


def initialize_time_service(configured_timezone: str = "system") -> TimeService:
    """在数据库配置加载后创建唯一进程时间上下文。"""

    global _time_service
    _time_service = TimeService(configured_timezone)
    return _time_service


def get_time_service() -> TimeService:
    global _time_service
    if _time_service is None:
        _time_service = TimeService("system")
    return _time_service


def now_utc() -> datetime:
    """共享 aware UTC 当前时间入口。"""

    return get_time_service().now_utc()


def monotonic() -> float:
    return get_time_service().monotonic()
