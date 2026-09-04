"""API v1 Pydantic 请求/响应模型"""

from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


# ========== 通用模型 ==========


class PaginatedResponse[T](BaseModel):
    """分页响应"""

    items: list[T]
    total: int
    page: int
    total_pages: int
    per_page: int


class ErrorResponse(BaseModel):
    """错误响应"""

    success: bool = False
    error: str
    detail: str | None = None
    code: str | None = None


class SuccessResponse(BaseModel):
    """成功响应（无数据）"""

    success: bool = True
    message: str = "ok"


# ========== 认证模型 ==========


class OAuthAuthorizeResponse(BaseModel):
    """OAuth 授权 URL 响应"""

    authorization_url: str
    state: str


class OAuthCallbackRequest(BaseModel):
    """OAuth 回调请求（移动端）"""

    code: str
    state: str


class TokenResponse(BaseModel):
    """Token 响应"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400
    user: UserInfoResponse


class MfaRequiredResponse(BaseModel):
    """需要二次验证的 OAuth 响应"""

    mfa_required: bool = True
    mfa_token: str
    methods: list[str] = Field(default_factory=lambda: ["totp", "recovery_code"])
    user: UserInfoResponse


class MfaVerifyRequest(BaseModel):
    """二次验证请求"""

    mfa_token: str
    code: str


class UserInfoResponse(BaseModel):
    """用户信息响应"""

    sub: str = Field(description="GitHub 用户名")
    role: str
    user_id: int
    github_id: int | None = None
    avatar_url: str | None = None
    email: str | None = None
    email_verified: bool = False


# ========== 审查模型 ==========


class ReviewCommentResponse(BaseModel):
    """审查评论响应"""

    id: int
    review_id: int
    file_path: str | None = None
    line_number: int | None = None
    comment_type: str | None = None
    severity: str | None = None
    content: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class ReviewResponse(BaseModel):
    """PR 审查响应"""

    id: int
    pr_id: int
    repo_name: str | None = None
    repo_owner: str | None = None
    author: str | None = None
    title: str | None = None
    branch: str | None = None
    file_count: int | None = None
    line_count: int | None = None
    code_file_count: int | None = None
    strategy: str | None = None
    status: str | None = None
    error_message: str | None = None
    review_summary: str | None = None
    overall_score: int | None = None
    decision: str | None = None
    decision_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class ReviewFileStatsResponse(BaseModel):
    """审查文件统计响应"""

    file_path: str
    severity_counts: dict[str, int]
    comment_count: int


# ========== Issue 模型 ==========


class IssueAnalysisResponse(BaseModel):
    """Issue 分析响应"""

    id: int
    issue_number: int
    repo_name: str | None = None
    repo_owner: str | None = None
    author: str | None = None
    title: str | None = None
    category: str | None = None
    priority: str | None = None
    summary: str | None = None
    feasibility: str | None = None
    suggested_title: str | None = None
    suggested_assignees: str | None = None
    suggested_labels: str | None = None
    suggested_milestone: str | None = None
    duplicate_of: int | None = None
    related_prs: str | None = None
    analysis_detail: str | None = None
    status: str | None = None
    error_message: str | None = None
    comment_posted: int | None = None
    comment_url: str | None = None
    labels_applied: int | None = None
    applied_label_names: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class IssueStatsResponse(BaseModel):
    """Issue 统计响应"""

    total: int
    by_category: dict[str, int]
    by_priority: dict[str, int]
    by_status: dict[str, int]


# ========== 用户模型 ==========


class UserResponse(BaseModel):
    """用户响应"""

    id: int
    # Telegram is an optional notification endpoint.  Keep this nullable so
    # GitHub/Passkey-only accounts can be listed before they opt in to chat
    # notifications; legacy rows continue to expose their positive id.
    telegram_id: int | None = None
    github_username: str | None = None
    role: str
    daily_quota: int | None = None
    weekly_quota: int | None = None
    monthly_quota: int | None = None
    daily_used: int | None = None
    weekly_used: int | None = None
    monthly_used: int | None = None
    issue_daily_quota: int | None = None
    issue_weekly_quota: int | None = None
    issue_monthly_quota: int | None = None
    issue_daily_used: int | None = None
    issue_weekly_used: int | None = None
    issue_monthly_used: int | None = None
    is_active: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserCreateRequest(BaseModel):
    """创建用户请求"""

    telegram_id: int | None = None
    github_username: str
    role: str = "user"
    daily_quota: int = 10
    weekly_quota: int = 50
    monthly_quota: int = 200
    issue_daily_quota: int = 20
    issue_weekly_quota: int = 80
    issue_monthly_quota: int = 300


class UserRoleUpdateRequest(BaseModel):
    """更新用户角色请求"""

    role: str


class UserQuotaUpdateRequest(BaseModel):
    """更新用户配额请求"""

    daily_quota: int
    weekly_quota: int
    monthly_quota: int


class UserIssueQuotaUpdateRequest(BaseModel):
    """更新用户 Issue 配额请求"""

    issue_daily_quota: int
    issue_weekly_quota: int
    issue_monthly_quota: int


class UserInfoUpdateRequest(BaseModel):
    """更新用户基本信息请求"""

    telegram_id: int | None = None
    github_username: str


# ========== 仓库模型 ==========


class RepoResponse(BaseModel):
    """仓库响应"""

    repo_name: str
    is_active: bool
    added_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# ========== 扫描模型 ==========


class ScanFindingResponse(BaseModel):
    """扫描发现响应"""

    id: int
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    severity: str | None = None
    category: str | None = None
    title: str | None = None
    description: str | None = None
    suggestion: str | None = None
    confidence: int | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class ScanResponse(BaseModel):
    """扫描响应"""

    id: int
    repo_name: str | None = None
    repo_owner: str | None = None
    trigger_type: str | None = None
    triggered_by: str | None = None
    commit_sha: str | None = None
    file_count: int | None = None
    code_file_count: int | None = None
    status: str | None = None
    progress: int | None = None
    current_phase: str | None = None
    error_message: str | None = None
    total_findings: int | None = None
    critical_count: int | None = None
    major_count: int | None = None
    minor_count: int | None = None
    suggestion_count: int | None = None
    overall_health_score: int | None = None
    report_issue_number: int | None = None
    report_issue_url: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    findings: list[ScanFindingResponse] | None = None

    model_config = {"from_attributes": True}


class ScanStatsResponse(BaseModel):
    """扫描统计响应"""

    total: int
    by_status: dict[str, int]
    avg_health_score: float | None = None


# ========== 队列模型 ==========


class QueueItemResponse(BaseModel):
    """队列项响应"""

    id: int
    pr_id: int | None = None
    repo_name: str | None = None
    action: str | None = None
    priority: int | None = None
    status: str | None = None
    retry_count: int | None = None
    max_retries: int | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    processed_at: datetime | None = None

    model_config = {"from_attributes": True}


class QueueStatsResponse(BaseModel):
    """队列统计响应"""

    pending: int
    processing: int
    completed: int
    failed: int
    total: int


# ========== 配置模型 ==========


UserConfigValue = str | int | bool | None


class ConfigGeneralResponse(BaseModel):
    """通用配置响应"""

    configs: dict[str, Any]


class ConfigGeneralUpdateRequest(BaseModel):
    """更新通用配置请求"""

    configs: dict[str, str]


class UserConfigUpdateRequest(BaseModel):
    """更新用户级配置请求。"""

    model_config = ConfigDict(extra="forbid")

    configs: dict[str, UserConfigValue]


class ConfigStrategiesResponse(BaseModel):
    """策略配置响应"""

    strategies: dict[str, Any]


class ConfigStrategyUpdateRequest(BaseModel):
    """更新策略配置请求"""

    section: str
    data: dict[str, Any]


class ConfigLabelsResponse(BaseModel):
    """标签配置响应"""

    labels: list[dict[str, Any]]
    recommendation: dict[str, Any]


class ConfigLabelsUpdateRequest(BaseModel):
    """更新标签定义请求"""

    labels: list[dict[str, Any]]


class ConfigLabelRecommendationUpdateRequest(BaseModel):
    """更新标签推荐设置请求"""

    recommendation: dict[str, Any]


# ========== 日志模型 ==========


class AdminActionLogResponse(BaseModel):
    """操作日志响应"""

    id: int
    admin_id: int | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    detail: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# ========== 设置模型 ==========


class SettingsResponse(BaseModel):
    """个人设置响应"""

    theme: str | None = None
    language: str | None = None
    items_per_page: int | None = None


class SettingsUpdateRequest(BaseModel):
    """更新个人设置请求"""

    items_per_page: int | None = None


# ========== 仪表盘模型 ==========


class DashboardStatsResponse(BaseModel):
    """仪表盘统计响应"""

    total_reviews: int
    completed_reviews: int
    avg_score: float | None = None
    avg_duration: float | None = None
    total_issues: int
    total_scans: int


class DashboardChartDataResponse(BaseModel):
    """仪表盘图表数据响应"""

    labels: list[str]
    datasets: list[dict[str, Any]]
