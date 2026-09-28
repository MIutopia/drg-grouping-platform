-- =====================================================================
-- 01  Create the project database.
-- Idempotent: safe to re-run. Collation is inherited from the instance so
-- joins against SBO / med_record keep working without conversions.
-- =====================================================================
IF DB_ID(N'drg') IS NULL
BEGIN
    CREATE DATABASE [drg];
    PRINT N'created database drg';
END
ELSE
BEGIN
    PRINT N'database drg already exists';
END
GO
