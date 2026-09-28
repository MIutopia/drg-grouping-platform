-- =====================================================================
-- 08  dict_policy_doc / dict_policy_fact: Chinese columns were declared varchar.
--
-- 07 declared doc_key / category as varchar, but doc_key is built from the
-- Chinese file name. Under the instance collation (Chinese_PRC_CI_AS) a Chinese
-- character needs two bytes, so a 60-character key needs 120 bytes and blew past
-- varchar(80) - the load failed with 8152 "String or binary data would be
-- truncated", the same trap 06_rebuild_for_charset.sql fixed elsewhere.
-- Any column that can hold Chinese must be nvarchar.
-- =====================================================================
USE [drg];
GO

ALTER TABLE dbo.dict_policy_doc ALTER COLUMN doc_key nvarchar(120) NOT NULL;
GO

ALTER TABLE dbo.dict_policy_doc ALTER COLUMN category nvarchar(8) NULL;
GO

ALTER TABLE dbo.dict_policy_fact ALTER COLUMN doc_key nvarchar(120) NOT NULL;
GO
