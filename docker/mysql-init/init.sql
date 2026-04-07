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
('app_version', '2.7.2', '应用版本号'),
('max_concurrent_reviews', '5', '最大并发审查数量'),
('review_timeout_seconds', '300', '审查超时时间（秒）'),
('enable_auto_review', 'true', '是否启用自动审查'),
('issue_auto_create_labels', 'true', '自动为 Issue 应用 AI 推荐的标签'),
('issue_auto_assign', 'true', '自动为 Issue 指派 AI 推荐的负责人'),
('issue_max_tool_iterations', '15', 'Issues 分析中 AI 工具调用最大迭代次数');
