"""API v1 Pydantic 请求/响应模型"""

from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ========== 通用模型 ==========


class PaginatedResponse(BaseModel, Generic[T]):
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
    detail: Optional[str] = None
    code: Optional[str] = None


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
    user: "UserInfoResponse"


class UserInfoResponse(BaseModel):
    """用户信息响应"""

    sub: str = Field(description="GitHub 用户名")
    role: str
    user_id: int
    github_id: Optional[int] = None
    avatar_url: Optional[str] = None


# ========== 审查模型 ==========


class ReviewCommentResponse(BaseModel):
    """审查评论响应"""

    id: int
    review_id: int
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    comment_type: Optional[str] = None
    severity: Optional[str] = None
    content: Optional[str] = None
    fix_suggestion: Optional[str] = None
    fix_confidence: Optional[float] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ReviewResponse(BaseModel):
    """PR 审查响应"""

    id: int
    pr_id: int
    repo_name: Optional[str] = None
    repo_owner: Optional[str] = None
    author: Optional[str] = None
    title: Optional[str] = None
    branch: Optional[str] = None
    file_count: Optional[int] = None
    line_count: Optional[int] = None
    code_file_count: Optional[int] = None
    strategy: Optional[str] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    review_summary: Optional[str] = None
    overall_score: Optional[int] = None
    decision: Optional[str] = None
    decision_reason: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    estimated_cost: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

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
    repo_name: Optional[str] = None
    repo_owner: Optional[str] = None
    author: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[str] = None
    summary: Optional[str] = None
    feasibility: Optional[str] = None
    suggested_title: Optional[str] = None
    suggested_assignees: Optional[str] = None
    suggested_labels: Optional[str] = None
    suggested_milestone: Optional[str] = None
    duplicate_of: Optional[int] = None
    related_prs: Optional[str] = None
    analysis_detail: Optional[str] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    comment_posted: Optional[int] = None
    comment_url: Optional[str] = None
    labels_applied: Optional[int] = None
    applied_label_names: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

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
    telegram_id: int
    github_username: Optional[str] = None
    role: str
    daily_quota: Optional[int] = None
    weekly_quota: Optional[int] = None
    monthly_quota: Optional[int] = None
    daily_used: Optional[int] = None
    weekly_used: Optional[int] = None
    monthly_used: Optional[int] = None
    issue_daily_quota: Optional[int] = None
    issue_weekly_quota: Optional[int] = None
    issue_monthly_quota: Optional[int] = None
    issue_daily_used: Optional[int] = None
    issue_weekly_used: Optional[int] = None
    issue_monthly_used: Optional[int] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class UserCreateRequest(BaseModel):
    """创建用户请求"""

    telegram_id: int
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

    telegram_id: int
    github_username: str


# ========== 仓库模型 ==========


class RepoResponse(BaseModel):
    """仓库响应"""

    repo_name: str
    is_active: bool
    added_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ========== 扫描模型 ==========


class ScanFindingResponse(BaseModel):
    """扫描发现响应"""

    id: int
    file_path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    severity: Optional[str] = None
    category: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    suggestion: Optional[str] = None
    confidence: Optional[int] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ScanResponse(BaseModel):
    """扫描响应"""

    id: int
    repo_name: Optional[str] = None
    repo_owner: Optional[str] = None
    trigger_type: Optional[str] = None
    triggered_by: Optional[str] = None
    commit_sha: Optional[str] = None
    file_count: Optional[int] = None
    code_file_count: Optional[int] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    current_phase: Optional[str] = None
    error_message: Optional[str] = None
    total_findings: Optional[int] = None
    critical_count: Optional[int] = None
    major_count: Optional[int] = None
    minor_count: Optional[int] = None
    suggestion_count: Optional[int] = None
    overall_health_score: Optional[int] = None
    report_issue_number: Optional[int] = None
    report_issue_url: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    findings: Optional[list[ScanFindingResponse]] = None

    model_config = {"from_attributes": True}


class ScanStatsResponse(BaseModel):
    """扫描统计响应"""

    total: int
    by_status: dict[str, int]
    avg_health_score: Optional[float] = None


# ========== 队列模型 ==========


class QueueItemResponse(BaseModel):
    """队列项响应"""

    id: int
    pr_id: Optional[int] = None
    repo_name: Optional[str] = None
    action: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    retry_count: Optional[int] = None
    max_retries: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    processed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class QueueStatsResponse(BaseModel):
    """队列统计响应"""

    pending: int
    processing: int
    completed: int
    failed: int
    total: int


# ========== 配置模型 ==========


class ConfigGeneralResponse(BaseModel):
    """通用配置响应"""

    configs: dict[str, Any]


class ConfigGeneralUpdateRequest(BaseModel):
    """更新通用配置请求"""

    configs: dict[str, str]


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
    admin_id: Optional[int] = None
    action: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    detail: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ========== 设置模型 ==========


class SettingsResponse(BaseModel):
    """个人设置响应"""

    theme: Optional[str] = None
    language: Optional[str] = None
    items_per_page: Optional[int] = None


class SettingsUpdateRequest(BaseModel):
    """更新个人设置请求"""

    items_per_page: Optional[int] = None


# ========== 仪表盘模型 ==========


class DashboardStatsResponse(BaseModel):
    """仪表盘统计响应"""

    total_reviews: int
    completed_reviews: int
    avg_score: Optional[float] = None
    avg_duration: Optional[float] = None
    total_issues: int
    total_scans: int


class DashboardChartDataResponse(BaseModel):
    """仪表盘图表数据响应"""

    labels: list[str]
    datasets: list[dict[str, Any]]
