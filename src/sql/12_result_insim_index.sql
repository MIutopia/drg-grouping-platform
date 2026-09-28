-- =====================================================================
-- 12  Indexes for the two hot paths measured by the test stand.
--
-- Why (test report D6 / D13):
--   1. the doctor workbench (`/api/insim/mine`) filters result_insim by snapshot_date and
--      doctor_phone on every poll; without an index that is a scan of the whole snapshot;
--   2. the access audit grows continuously and is purged/queried by time (`sys_audit_log.at`)
--      and by user, so both access paths deserve an index before it gets large.
-- Both are additive and idempotent; no data is touched.
-- =====================================================================
USE [drg];
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_result_insim_snapshot_doctor'
                 AND object_id = OBJECT_ID(N'dbo.result_insim'))
CREATE INDEX IX_result_insim_snapshot_doctor
    ON dbo.result_insim (snapshot_date, doctor_phone)
    INCLUDE (hospital_no, days, drg_code);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_sys_audit_log_at'
                 AND object_id = OBJECT_ID(N'dbo.sys_audit_log'))
CREATE INDEX IX_sys_audit_log_at ON dbo.sys_audit_log (at);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_sys_audit_log_phone_at'
                 AND object_id = OBJECT_ID(N'dbo.sys_audit_log'))
CREATE INDEX IX_sys_audit_log_phone_at ON dbo.sys_audit_log (user_phone, at);
GO
