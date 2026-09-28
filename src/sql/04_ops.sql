-- =====================================================================
-- 04  Operations layer (drg.ops_*).
-- Backs the intelligent-O&M requirements: every DDL run, job run, source
-- freshness probe and alert is recorded here rather than only in a log file.
-- Idempotent: tables are only created when absent.
-- =====================================================================
USE [drg];
GO

-- ---- DDL audit: which script, which revision, when ---------------------
IF OBJECT_ID(N'dbo.ops_ddl_log', N'U') IS NULL
CREATE TABLE dbo.ops_ddl_log (
    id        int IDENTITY(1,1) PRIMARY KEY,
    script    nvarchar(200) NOT NULL,
    sha256    varchar(64)   NULL,
    batches   int           NULL,
    seconds   decimal(10,2) NULL,
    executed_at datetime    NOT NULL DEFAULT GETDATE(),
    executed_by nvarchar(100) NULL DEFAULT SUSER_SNAME()
);
GO

-- ---- job run log -------------------------------------------------------
IF OBJECT_ID(N'dbo.ops_run_log', N'U') IS NULL
CREATE TABLE dbo.ops_run_log (
    id          bigint IDENTITY(1,1) PRIMARY KEY,
    job_name    varchar(60)   NOT NULL,
    started_at  datetime      NOT NULL,
    ended_at    datetime      NULL,
    status      varchar(12)   NOT NULL,     -- ok | failed | running
    rows_out    int           NULL,
    message     nvarchar(1000) NULL,
    policy_version nvarchar(80) NULL
);
GO

-- ---- source freshness: measured from the DATA, not from SyncControl ----
-- Measured on 2026-09-20: med_record.SyncControl reported the homepage as
-- last synced 2026-08-19 while its newest row was 2026-09-19, so the control
-- table alone would produce a false "stalled" alert. Business timestamps win.
IF OBJECT_ID(N'dbo.ops_freshness', N'U') IS NULL
CREATE TABLE dbo.ops_freshness (
    id            bigint IDENTITY(1,1) PRIMARY KEY,
    observed_at   datetime      NOT NULL,
    source_db     varchar(40)   NOT NULL,
    source_table  varchar(80)   NOT NULL,
    business_ts   datetime      NULL,       -- MAX(business time) read from the data
    rows_total    bigint        NULL,
    lag_hours     decimal(10,2) NULL,
    sync_ctrl_ts  datetime      NULL,       -- SyncControl.LastSyncTime, for comparison
    status        varchar(20)   NOT NULL    -- ok | warn | alert | unknown
);
GO

-- ---- alerts ------------------------------------------------------------
IF OBJECT_ID(N'dbo.ops_alert', N'U') IS NULL
CREATE TABLE dbo.ops_alert (
    id          bigint IDENTITY(1,1) PRIMARY KEY,
    raised_at   datetime      NOT NULL DEFAULT GETDATE(),
    severity    varchar(12)   NOT NULL,     -- info | warn | high
    item        nvarchar(80)  NOT NULL,
    detail      nvarchar(1000) NULL,
    status      varchar(16)   NOT NULL DEFAULT 'open',   -- open | ack | closed
    acked_by    nvarchar(100) NULL,
    acked_at    datetime      NULL
);
GO

-- Several jobs share this table, so each one must be able to refresh its OWN open alerts.
-- Added after s19 and s20 were found to clear each other's alerts: the original refresh was
-- "DELETE WHERE status='open'", which is correct for one producer and wrong for two.
IF COL_LENGTH('dbo.ops_alert', 'owner') IS NULL
ALTER TABLE dbo.ops_alert ADD [owner] varchar(20) NULL;
GO

-- ---- regression gate: one row per full back-test run -------------------
-- Backs the "change -> one-click regression -> threshold verdict" loop in docs/24.
-- The thresholds are stored per run rather than as constants in a report, so history shows
-- which bar actually applied at the time: a grouping-scheme revision legitimately moves them,
-- and a past verdict must stay reproducible against the bar it was judged by.
IF OBJECT_ID(N'dbo.ops_regress_run', N'U') IS NULL
CREATE TABLE dbo.ops_regress_run (
    run_id         bigint IDENTITY(1,1) PRIMARY KEY,
    run_at         datetime       NOT NULL DEFAULT GETDATE(),
    policy_version nvarchar(80)   NULL,
    cases          int            NULL,
    adrg_ok        int            NULL,
    adrg_rate      decimal(9,6)   NULL,
    drg_ok         int            NULL,
    drg_rate       decimal(9,6)   NULL,
    vendor_rate    decimal(9,6)   NULL,      -- vendor engine ADRG rate, for the standing comparison
    th_adrg        decimal(9,6)   NULL,      -- threshold in force for this run
    th_drg         decimal(9,6)   NULL,
    verdict        varchar(12)    NOT NULL,  -- pass | warn | fail
    delta_adrg     decimal(9,6)   NULL,      -- percentage points vs the previous run
    delta_drg      decimal(9,6)   NULL,
    note           nvarchar(1000) NULL
);
GO

-- ---- regression metrics per dimension ----------------------------------
-- dim='ALL' is the run headline; dim='YYYY-MM' gives the rolling monthly series that the
-- reverse-detection rule in docs/24 section 3 reads (an unexpected month-on-month drop is the
-- signal that the payer changed something without issuing a document).
IF OBJECT_ID(N'dbo.ops_regress_metric', N'U') IS NULL
CREATE TABLE dbo.ops_regress_metric (
    id         bigint IDENTITY(1,1) PRIMARY KEY,
    run_id     bigint       NOT NULL,
    dim        varchar(16)  NOT NULL,        -- ALL | 2026-03 ...
    cases      int          NULL,
    adrg_ok    int          NULL,
    adrg_rate  decimal(9,6) NULL,
    drg_ok     int          NULL,
    drg_rate   decimal(9,6) NULL,
    tcm_cases  int          NULL,
    std_cases  int          NULL,
    none_cases int          NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_ops_regress_metric_run')
CREATE INDEX IX_ops_regress_metric_run ON dbo.ops_regress_metric(run_id, dim);
GO

-- ---- console-triggered job runs (docs/29 运维面板) ---------------------
-- One row per trigger from the IT console. Persisted here instead of only in process memory so
-- the history survives a restart; retention is capped by s28_joblog_purge.py (30 days default).
-- Distinct from ops_run_log, which the ETL jobs write for themselves.
IF OBJECT_ID(N'dbo.ops_job_run', N'U') IS NULL
CREATE TABLE dbo.ops_job_run (
    id           bigint IDENTITY(1,1) PRIMARY KEY,
    run_id       varchar(32)   NOT NULL,       -- 控制台返回的运行号
    job_key      varchar(24)   NOT NULL,
    job_label    nvarchar(120) NULL,
    status       varchar(16)   NOT NULL,       -- running | ok | fail | timeout | error
    returncode   int           NULL,
    started_at   datetime      NOT NULL,
    ended_at     datetime      NULL,
    triggered_by nvarchar(60)  NULL,           -- 触发人
    log          nvarchar(max) NULL,           -- 作业 stdout/stderr 尾部
    CONSTRAINT UQ_ops_job_run_run_id UNIQUE (run_id)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_ops_job_run_started')
CREATE INDEX IX_ops_job_run_started ON dbo.ops_job_run(started_at DESC);
GO
