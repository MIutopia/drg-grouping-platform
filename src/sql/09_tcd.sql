-- =====================================================================
-- 09  TCD mapping (drg.dict_tcd_map).
-- Official TCM disease/syndrome classification (GB/T 15657-2021), extracted by S12 from
-- 27号文 附件5. This is the dictionary that backs the ④ 编码版本类 change path in docs/24:
-- when the national TCD standard is revised, this is the table to update. It had no home
-- before (the mapping lived only as out/tcd/*.csv), so a TCD revision had nothing to change.
-- Idempotent: the table is only created when absent.
-- =====================================================================
USE [drg];
GO

IF OBJECT_ID(N'dbo.dict_tcd_map', N'U') IS NULL
CREATE TABLE dbo.dict_tcd_map (
    source         nvarchar(60)   NULL,      -- 细分组目录-附件5
    name           nvarchar(120)  NULL,      -- 病种/证型名称, e.g. 中风/卒中
    tcd_code       varchar(40)    NOT NULL,  -- e.g. A07.01.01.
    name_key       nvarchar(120)  NULL,      -- 名称去噪后的匹配键
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_tcd_map PRIMARY KEY (tcd_code, policy_version)
);
GO
