-- =====================================================================
-- 06  Migration: rebuild for the encoding defect.
--
-- Defect: the instance collation is Chinese_PRC_CI_AS while the driver connects with
-- charset=utf8, so Chinese written into a VARCHAR column is corrupted on the way in.
-- NVARCHAR columns are unaffected. Detected by s19_freshness.py --audit, which found 21
-- affected columns - including every policy_version, the field that carries the year-end
-- settlement evidence.
--
-- Fix: 02/03/04 declare the affected columns as NVARCHAR. The data itself is fine at the
-- source (src/config/dict, src/out), so the tables are dropped and rebuilt rather than
-- altered: most of them carry policy_version inside the primary key, which would require
-- dropping and recreating the key anyway. ops_ddl_log is kept because its columns are ASCII
-- and it holds the migration trail.
-- =====================================================================
USE [drg];
GO

-- dictionary layer
IF OBJECT_ID(N'dbo.dict_adrg_cond', N'U') IS NOT NULL DROP TABLE dbo.dict_adrg_cond;
GO
IF OBJECT_ID(N'dbo.dict_adrg_name', N'U') IS NOT NULL DROP TABLE dbo.dict_adrg_name;
GO
IF OBJECT_ID(N'dbo.dict_cost_item_attr', N'U') IS NOT NULL DROP TABLE dbo.dict_cost_item_attr;
GO
IF OBJECT_ID(N'dbo.dict_drg', N'U') IS NOT NULL DROP TABLE dbo.dict_drg;
GO
IF OBJECT_ID(N'dbo.dict_drug_hint', N'U') IS NOT NULL DROP TABLE dbo.dict_drug_hint;
GO
IF OBJECT_ID(N'dbo.dict_group_catalog', N'U') IS NOT NULL DROP TABLE dbo.dict_group_catalog;
GO
IF OBJECT_ID(N'dbo.dict_homepage_op_class', N'U') IS NOT NULL DROP TABLE dbo.dict_homepage_op_class;
GO
IF OBJECT_ID(N'dbo.dict_main_dx', N'U') IS NOT NULL DROP TABLE dbo.dict_main_dx;
GO
IF OBJECT_ID(N'dbo.dict_mdcz_dx', N'U') IS NOT NULL DROP TABLE dbo.dict_mdcz_dx;
GO
IF OBJECT_ID(N'dbo.dict_no_main', N'U') IS NOT NULL DROP TABLE dbo.dict_no_main;
GO
IF OBJECT_ID(N'dbo.dict_qy_group', N'U') IS NOT NULL DROP TABLE dbo.dict_qy_group;
GO
IF OBJECT_ID(N'dbo.dict_severity', N'U') IS NOT NULL DROP TABLE dbo.dict_severity;
GO
IF OBJECT_ID(N'dbo.dict_tcm_group', N'U') IS NOT NULL DROP TABLE dbo.dict_tcm_group;
GO
IF OBJECT_ID(N'dbo.dict_tcm_group_dx', N'U') IS NOT NULL DROP TABLE dbo.dict_tcm_group_dx;
GO
IF OBJECT_ID(N'dbo.dict_tcm_group_op', N'U') IS NOT NULL DROP TABLE dbo.dict_tcm_group_op;
GO
IF OBJECT_ID(N'dbo.param_settlement', N'U') IS NOT NULL DROP TABLE dbo.param_settlement;
GO

-- result layer
IF OBJECT_ID(N'dbo.result_coding_counterfactual', N'U') IS NOT NULL DROP TABLE dbo.result_coding_counterfactual;
GO
IF OBJECT_ID(N'dbo.result_coding_finding', N'U') IS NOT NULL DROP TABLE dbo.result_coding_finding;
GO
IF OBJECT_ID(N'dbo.result_compare_case', N'U') IS NOT NULL DROP TABLE dbo.result_compare_case;
GO
IF OBJECT_ID(N'dbo.result_engine_pred', N'U') IS NOT NULL DROP TABLE dbo.result_engine_pred;
GO
IF OBJECT_ID(N'dbo.result_insim', N'U') IS NOT NULL DROP TABLE dbo.result_insim;
GO
IF OBJECT_ID(N'dbo.result_pnl', N'U') IS NOT NULL DROP TABLE dbo.result_pnl;
GO
IF OBJECT_ID(N'dbo.result_recon_case', N'U') IS NOT NULL DROP TABLE dbo.result_recon_case;
GO
IF OBJECT_ID(N'dbo.result_settlement_return', N'U') IS NOT NULL DROP TABLE dbo.result_settlement_return;
GO

-- operations layer (ops_ddl_log is intentionally kept: ASCII-only, and it is the audit trail)
IF OBJECT_ID(N'dbo.ops_alert', N'U') IS NOT NULL DROP TABLE dbo.ops_alert;
GO
IF OBJECT_ID(N'dbo.ops_freshness', N'U') IS NOT NULL DROP TABLE dbo.ops_freshness;
GO
IF OBJECT_ID(N'dbo.ops_run_log', N'U') IS NOT NULL DROP TABLE dbo.ops_run_log;
GO

PRINT N'rebuild_for_charset done - run 02/03/04 then --load';
GO
