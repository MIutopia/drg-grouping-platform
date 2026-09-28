-- =====================================================================
-- 05  Migration: rebuild dict_main_dx with a composite key that includes mdc.
--
-- Why: 14 diagnosis codes belong to two MDCs at once (C49.503 会阴结缔组织恶性肿瘤 and
-- similar perineal/genital codes are MDCM for a male and MDCN for a female). The original
-- key (code, policy_version) therefore rejected the second insert and the load stopped
-- halfway. 02_dict.sql carries the corrected definition; this script only drops the old
-- table so the next --create rebuilds it.
--
-- Scope: touches drg.dbo.dict_main_dx only. Dictionary tables are rebuilt from source, so
-- dropping this one loses nothing.
-- =====================================================================
USE [drg];
GO

IF OBJECT_ID(N'dbo.dict_main_dx', N'U') IS NOT NULL
BEGIN
    DROP TABLE dbo.dict_main_dx;
    PRINT N'dict_main_dx dropped - run 02_dict.sql to rebuild';
END
ELSE
BEGIN
    PRINT N'dict_main_dx does not exist - nothing to do';
END
GO
