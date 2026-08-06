-- Sakura AI MySQL 初始化（供需要自定义字符集/排序规则的场景手动挂载）
-- 生产 compose 不挂载本文件：数据库与用户由 MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD 创建，
-- 表结构由应用启动时 Base.metadata.create_all + _auto_migrate 自动创建/迁移。

CREATE DATABASE IF NOT EXISTS `sakura_ai` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
