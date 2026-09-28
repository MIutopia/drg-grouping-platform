-- =====================================================================
-- 10  Widen ops_alert.severity so an unknown alert level never loses a row.
--
-- Why (test report D4, docs/35):
--   1. the column was varchar(12); an upstream introducing a longer level - or any writer
--      passing one - failed the WHOLE insert with error 2628 "String or binary data would
--      be truncated". A monitoring table must degrade to "recorded", never to "lost";
--   2. it was varchar, and this instance's collation is Chinese_PRC_CI_AS while the driver
--      connects with charset=utf8, so Chinese written into varchar comes back mangled
--      (same defect class as docs/25). Severity may legitimately carry Chinese.
-- nvarchar(32) fixes both. Unknown values are additionally normalised on the write path
-- (ddlio.insert_alert maps them to 'warn' and keeps the original text in detail).
--
-- Idempotent: skipped once the column is already nvarchar and wide enough.
-- =====================================================================
USE [drg];
GO

IF EXISTS (SELECT 1 FROM sys.columns c
           JOIN sys.types t ON t.user_type_id = c.user_type_id
           WHERE c.object_id = OBJECT_ID(N'dbo.ops_alert') AND c.name = N'severity'
             AND (t.name <> N'nvarchar' OR c.max_length < 64))
ALTER TABLE dbo.ops_alert ALTER COLUMN severity nvarchar(32) NOT NULL;
GO
