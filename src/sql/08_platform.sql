-- =====================================================================
-- 08  Platform foundation tables (drg.sys_role / sys_user / sys_doctor /
--     sys_audit_log).
--
-- Why this exists: the web workbench platform (docs/29) is a multi-role
-- application, not a reporting tool. That forces three things the BI path
-- could leave fuzzy: (1) login/auth with roles, (2) a per-physician identity
-- that "个人工作台" scopes by, and (3) an access audit trail - the gap that
-- docs/28 scored at 75%.
--
-- sys_doctor is the mobile-number mapping the hospital decided on: the
-- responsible physician is the 住院医师, identified by mobile number instead
-- of HIS job number. HIS SYS_USER.列(phone_mobi) is empty (4/469 rows), so the
-- numbers come from an HR export and are loaded here, NOT read from HIS.
-- sys_doctor is created empty and populated by the import job when HR delivers
-- the export; until then ETL keeps writing the raw "工号|姓名" string.
--
-- Lower-case "sys_" prefix keeps these apart from the HIS database's own
-- SYS_USER / SYS_TELBOOK tables (uppercase), which this platform does NOT read.
-- Idempotent: tables are only created when absent.
-- =====================================================================
USE [drg];
GO

-- ---- role dictionary --------------------------------------------------
IF OBJECT_ID(N'dbo.sys_role', N'U') IS NULL
CREATE TABLE dbo.sys_role (
    role_id    int IDENTITY(1,1) PRIMARY KEY,
    role_code  varchar(20)  NOT NULL UNIQUE,   -- doctor / finance / cashier / it / manager
    role_name  nvarchar(40) NOT NULL
);
GO

-- Seed the fixed roles once (guarded so re-running the script is a no-op).
IF NOT EXISTS (SELECT 1 FROM dbo.sys_role WHERE role_code = 'doctor')
    INSERT dbo.sys_role (role_code, role_name) VALUES ('doctor', N'医师');
IF NOT EXISTS (SELECT 1 FROM dbo.sys_role WHERE role_code = 'finance')
    INSERT dbo.sys_role (role_code, role_name) VALUES ('finance', N'财务');
IF NOT EXISTS (SELECT 1 FROM dbo.sys_role WHERE role_code = 'cashier')
    INSERT dbo.sys_role (role_code, role_name) VALUES ('cashier', N'收费处');
IF NOT EXISTS (SELECT 1 FROM dbo.sys_role WHERE role_code = 'it')
    INSERT dbo.sys_role (role_code, role_name) VALUES ('it', N'信息科');
IF NOT EXISTS (SELECT 1 FROM dbo.sys_role WHERE role_code = 'manager')
    INSERT dbo.sys_role (role_code, role_name) VALUES ('manager', N'管理者/医保办');
GO

-- ---- login users (mobile number is the login name) --------------------
IF OBJECT_ID(N'dbo.sys_user', N'U') IS NULL
CREATE TABLE dbo.sys_user (
    user_id       int IDENTITY(1,1) PRIMARY KEY,
    login_phone   varchar(20)  NOT NULL UNIQUE,   -- 手机号即登录名
    user_name     nvarchar(40) NOT NULL,
    role_id       int          NOT NULL,          -- -> sys_role.role_id
    office        nvarchar(60) NULL,              -- 科室（冗余，便于列表展示）
    pwd_hash      varchar(200) NULL,              -- 密码哈希（首次登录强制设置）
    active        tinyint      NOT NULL DEFAULT 1,
    created_at    datetime     NOT NULL DEFAULT GETDATE(),
    last_login_at datetime     NULL
);
GO

-- ---- physician mapping from the HR export -----------------------------
-- doctor_phone is the identity and login name; doctor_code is the join key
-- back to HIS (the "工号|姓名" prefix of 列(doctor_residency)).
IF OBJECT_ID(N'dbo.sys_doctor', N'U') IS NULL
CREATE TABLE dbo.sys_doctor (
    doctor_phone  varchar(20)  PRIMARY KEY,        -- 手机号：唯一标识 + 登录名
    doctor_code   varchar(20)  NOT NULL UNIQUE,    -- 工号（对应 列(doctor_residency) 前缀）
    doctor_name   nvarchar(40) NOT NULL,
    office        nvarchar(60) NULL,               -- 科室
    title         nvarchar(40) NULL,               -- 职务/职称（住院医师等）
    active        tinyint      NOT NULL DEFAULT 1,
    source        nvarchar(60) NULL,               -- 导入来源（人事导出）
    updated_at    datetime     NOT NULL DEFAULT GETDATE()
);
GO

-- ---- access / action audit trail --------------------------------------
-- Every login, page view, export, alert ack, job trigger and parameter change
-- lands here, so "who did what when" is answerable for the audit-facing docs.
IF OBJECT_ID(N'dbo.sys_audit_log', N'U') IS NULL
CREATE TABLE dbo.sys_audit_log (
    id         bigint IDENTITY(1,1) PRIMARY KEY,
    user_phone varchar(20)  NULL,
    action     varchar(24)  NOT NULL,   -- login/logout/view/export/ack_alert/run_job/change_param/user_admin
    target     nvarchar(200) NULL,
    detail     nvarchar(500) NULL,
    ip         varchar(45)  NULL,
    at         datetime     NOT NULL DEFAULT GETDATE()
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_sys_audit_log_at')
CREATE INDEX IX_sys_audit_log_at ON dbo.sys_audit_log(at, user_phone);
GO
