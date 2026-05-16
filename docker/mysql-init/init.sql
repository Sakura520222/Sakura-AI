-- Sakura AI Reviewer 数据库初始化脚本

CREATE DATABASE IF NOT EXISTS `sakura-pr` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE `sakura-pr`;

-- 创建PR审查记录表
CREATE TABLE IF NOT EXISTS pr_reviews (
    id INT AUTO_INCREMENT PRIMARY KEY,
    pr_id BIGINT NOT NULL,
    repo_name VARCHAR(255) NOT NULL,
    repo_owner VARCHAR(100) NOT NULL,
    author VARCHAR(100),
    title VARCHAR(500),
    branch VARCHAR(100),
    file_count INT,
    line_count INT,
    code_file_count INT,
    strategy ENUM('quick', 'standard', 'deep', 'large', 'skip') NOT NULL,
    status ENUM('pending', 'reviewing', 'completed', 'failed') NOT NULL DEFAULT 'pending',
    error_message TEXT,
    review_summary TEXT,
    overall_score INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
    completed_at TIMESTAMP NULL,
    INDEX idx_pr_id (pr_id),
    INDEX idx_repo (repo_name),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建审查评论表
CREATE TABLE IF NOT EXISTS review_comments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    review_id INT NOT NULL,
    file_path VARCHAR(500),
    line_number INT,
    comment_type ENUM('overall', 'file', 'line') NOT NULL DEFAULT 'overall',
    severity ENUM('critical', 'major', 'minor', 'suggestion') NOT NULL DEFAULT 'suggestion',
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    FOREIGN KEY (review_id) REFERENCES pr_reviews(id) ON DELETE CASCADE,
    INDEX idx_review_id (review_id),
    INDEX idx_severity (severity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建应用配置表
CREATE TABLE IF NOT EXISTS app_config (
    id INT AUTO_INCREMENT PRIMARY KEY,
    key_name VARCHAR(100) UNIQUE NOT NULL,
    key_value TEXT,
    description VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
    INDEX idx_key_name (key_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建用户级配置表
CREATE TABLE IF NOT EXISTS user_configs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    config_key VARCHAR(100) NOT NULL,
    config_value TEXT,
    description VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
    UNIQUE KEY uq_user_config_key (user_id, config_key),
    INDEX idx_user_id (user_id),
    INDEX idx_config_key (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建审查队列表
CREATE TABLE IF NOT EXISTS review_queue (
    id INT AUTO_INCREMENT PRIMARY KEY,
    pr_id BIGINT NOT NULL,
    repo_name VARCHAR(255) NOT NULL,
    action VARCHAR(50) NOT NULL,
    priority INT NOT NULL DEFAULT 10,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    retry_count INT NOT NULL DEFAULT 0,
    max_retries INT NOT NULL DEFAULT 3,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
    processed_at TIMESTAMP NULL,
    INDEX idx_pr_id (pr_id),
    INDEX idx_repo_name (repo_name),
    INDEX idx_status (status),
    INDEX idx_priority (priority),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 插入默认配置
INSERT IGNORE INTO app_config (key_name, key_value, description) VALUES
('app_version', '2.10.1', '应用版本号'),
('max_concurrent_reviews', '5', '最大并发审查数量'),
('review_timeout_seconds', '300', '审查超时时间（秒）'),
('enable_auto_review', 'true', '是否启用自动审查'),
('issue_auto_create_labels', 'true', '自动为 Issue 应用 AI 推荐的标签'),
('issue_auto_assign', 'true', '自动为 Issue 指派 AI 推荐的负责人'),
('issue_max_tool_iterations', '15', 'Issues 分析中 AI 工具调用最大迭代次数'),
('max_concurrent_issues', '3', '最大并发 Issue 分析数量'),
('enable_inline_comments', 'true', '是否启用行内评论');

-- 创建 Sakura 记忆系统状态表
CREATE TABLE IF NOT EXISTS sakura_memory_states (
    id INT AUTO_INCREMENT PRIMARY KEY,
    repo_full_name VARCHAR(255) UNIQUE NOT NULL,
    reflection_count INT NOT NULL DEFAULT 0,
    last_consolidation_at TIMESTAMP NULL,
    last_consolidation_count INT NULL,
    is_initialized TINYINT(1) NOT NULL DEFAULT 0,
    knowledge_extracted TINYINT(1) NOT NULL DEFAULT 0,
    last_sakura_md_sha VARCHAR(40) NULL,
    last_memory_md_sha VARCHAR(40) NULL,
    consolidation_interval INT NOT NULL DEFAULT 5,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
    INDEX idx_repo_full_name (repo_full_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent 专家团队任务表
CREATE TABLE IF NOT EXISTS agent_team_tasks (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source_type VARCHAR(50) NOT NULL,
    source_id INT NULL,
    source_issue_number BIGINT NULL,
    repo_full_name VARCHAR(255) NOT NULL,
    repo_owner VARCHAR(100) NOT NULL,
    repo_name VARCHAR(255) NOT NULL,
    title VARCHAR(500) NOT NULL,
    summary TEXT,
    priority VARCHAR(50),
    candidate_score INT NOT NULL DEFAULT 0,
    status VARCHAR(50) NOT NULL DEFAULT 'candidate',
    current_phase VARCHAR(50),
    branch_name VARCHAR(255),
    workspace_path VARCHAR(1000),
    base_branch VARCHAR(255),
    base_commit_sha VARCHAR(64),
    resume_count INT NOT NULL DEFAULT 0,
    failed_phase VARCHAR(50),
    failed_role VARCHAR(50),
    rate_limit_reset_at TIMESTAMP NULL,
    last_checkpoint_at TIMESTAMP NULL,
    pr_number BIGINT,
    pr_url VARCHAR(500),
    iteration_count INT NOT NULL DEFAULT 0,
    max_iterations INT NOT NULL DEFAULT 3,
    started_by VARCHAR(100),
    locked_by VARCHAR(100),
    ai_config_snapshot TEXT,
    prompt_tokens INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    estimated_cost INT DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
    started_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    INDEX idx_agent_team_tasks_source_type (source_type),
    INDEX idx_agent_team_tasks_source_id (source_id),
    INDEX idx_agent_team_tasks_source_issue_number (source_issue_number),
    INDEX idx_agent_team_tasks_repo_full_name (repo_full_name),
    INDEX idx_agent_team_tasks_priority (priority),
    INDEX idx_agent_team_tasks_candidate_score (candidate_score),
    INDEX idx_agent_team_tasks_status (status),
    INDEX idx_agent_team_tasks_pr_number (pr_number),
    INDEX idx_agent_team_tasks_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent 专家团队迭代记录表
CREATE TABLE IF NOT EXISTS agent_team_iterations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_id INT NOT NULL,
    iteration_number INT NOT NULL,
    fullstack_plan TEXT,
    fullstack_result TEXT,
    professional_review TEXT,
    review_passed INT NOT NULL DEFAULT 0,
    test_command TEXT,
    test_output TEXT,
    test_passed INT NOT NULL DEFAULT 0,
    diff_summary TEXT,
    decision VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    completed_at TIMESTAMP NULL,
    FOREIGN KEY (task_id) REFERENCES agent_team_tasks(id) ON DELETE CASCADE,
    INDEX idx_agent_team_iterations_task_id (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent 修改文件记录表
CREATE TABLE IF NOT EXISTS agent_team_patch_files (
    id INT AUTO_INCREMENT PRIMARY KEY,
    iteration_id INT NOT NULL,
    file_path VARCHAR(512) NOT NULL,
    change_type VARCHAR(50),
    additions INT DEFAULT 0,
    deletions INT DEFAULT 0,
    diff_summary TEXT,
    risk_level VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    FOREIGN KEY (iteration_id) REFERENCES agent_team_iterations(id) ON DELETE CASCADE,
    INDEX idx_agent_team_patch_files_iteration_id (iteration_id),
    INDEX idx_agent_team_patch_files_file_path (file_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent 会话记录表
CREATE TABLE IF NOT EXISTS agent_team_sessions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_id INT NOT NULL,
    iteration_number INT NOT NULL,
    role_name VARCHAR(50) NOT NULL,
    resume_index INT NOT NULL DEFAULT 0,
    status VARCHAR(50) NOT NULL DEFAULT 'running',
    model VARCHAR(255),
    tool_calls_count INT NOT NULL DEFAULT 0,
    last_seq INT NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    completed_at TIMESTAMP NULL,
    FOREIGN KEY (task_id) REFERENCES agent_team_tasks(id) ON DELETE CASCADE,
    INDEX idx_agent_team_sessions_task_id (task_id),
    INDEX idx_agent_team_sessions_iteration_number (iteration_number),
    INDEX idx_agent_team_sessions_role_name (role_name),
    INDEX idx_agent_team_sessions_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent messages 追加日志表
CREATE TABLE IF NOT EXISTS agent_team_messages (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id INT NOT NULL,
    seq INT NOT NULL,
    role VARCHAR(50) NOT NULL,
    content LONGTEXT,
    message_json LONGTEXT NOT NULL,
    tool_call_id VARCHAR(255),
    finish_reason VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    FOREIGN KEY (session_id) REFERENCES agent_team_sessions(id) ON DELETE CASCADE,
    UNIQUE KEY uq_agent_message_seq (session_id, seq),
    INDEX idx_agent_team_messages_session_id (session_id),
    INDEX idx_agent_team_messages_role (role),
    INDEX idx_agent_team_messages_tool_call_id (tool_call_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent 工具调用账本表
CREATE TABLE IF NOT EXISTS agent_team_tool_calls (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id INT NOT NULL,
    assistant_message_id INT NOT NULL,
    tool_call_id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    arguments_json LONGTEXT,
    arguments_hash VARCHAR(64),
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    result_message_id INT NULL,
    started_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    error_message TEXT,
    FOREIGN KEY (session_id) REFERENCES agent_team_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (assistant_message_id) REFERENCES agent_team_messages(id) ON DELETE CASCADE,
    FOREIGN KEY (result_message_id) REFERENCES agent_team_messages(id) ON DELETE SET NULL,
    INDEX idx_agent_team_tool_calls_session_id (session_id),
    INDEX idx_agent_team_tool_calls_assistant_message_id (assistant_message_id),
    INDEX idx_agent_team_tool_calls_tool_call_id (tool_call_id),
    INDEX idx_agent_team_tool_calls_status (status),
    INDEX idx_agent_team_tool_calls_result_message_id (result_message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent 任务反馈记录表
CREATE TABLE IF NOT EXISTS agent_team_feedback (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_id INT NOT NULL,
    source VARCHAR(50) NOT NULL,
    external_id VARCHAR(255),
    author VARCHAR(100),
    content TEXT NOT NULL,
    resolved INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    FOREIGN KEY (task_id) REFERENCES agent_team_tasks(id) ON DELETE CASCADE,
    INDEX idx_agent_team_feedback_task_id (task_id),
    INDEX idx_agent_team_feedback_source (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 创建 Agent Skills 元数据表
CREATE TABLE IF NOT EXISTS agent_skills (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(120) UNIQUE NOT NULL,
    description TEXT,
    when_to_use TEXT,
    version VARCHAR(100),
    source_type VARCHAR(50) NOT NULL DEFAULT 'upload',
    source_url TEXT,
    source_ref VARCHAR(255),
    source_path TEXT,
    install_path TEXT NOT NULL,
    enabled INT NOT NULL DEFAULT 1,
    content_hash VARCHAR(64) NOT NULL,
    file_count INT NOT NULL DEFAULT 1,
    allowed_tools TEXT,
    arguments TEXT,
    requires TEXT,
    created_by VARCHAR(100),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP NOT NULL,
    INDEX idx_agent_skills_slug (slug),
    INDEX idx_agent_skills_source_type (source_type),
    INDEX idx_agent_skills_enabled (enabled),
    INDEX idx_agent_skills_content_hash (content_hash),
    INDEX idx_agent_skills_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
