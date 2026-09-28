-- =====================================================================
-- 09  Soft-delete column for sys_user (15-day recoverable account deletion).
--
-- Why: 后台管理现在支持"删除账号"，但误删要有撤回窗口。采用软删除而不是
-- 物理 DELETE——deleted_at 非空即视为已删除，登录与 /me 仍按 active=1 过滤，
-- 因此软删除账号无法登录；列表端点只回显 15 天内的已删除账号，供信息科撤回。
-- 超过 15 天的已删除账号不再出现在列表，等同永久删除（如需彻底清除再跑物理删除）。
-- Idempotent: 列已存在则跳过。
-- =====================================================================
USE [drg];
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID(N'dbo.sys_user') AND name = N'deleted_at')
ALTER TABLE dbo.sys_user ADD deleted_at datetime NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_sys_user_deleted_at')
CREATE INDEX IX_sys_user_deleted_at ON dbo.sys_user(deleted_at);
GO
