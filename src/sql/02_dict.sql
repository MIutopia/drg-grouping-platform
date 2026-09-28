-- =====================================================================
-- 02  Dictionary and parameter layer (drg.dict_*, drg.param_*).
-- Every table carries policy_version so a dictionary revision can coexist
-- with the previous one - required for year-end settlement evidence.
-- Idempotent: tables are only created when absent.
-- =====================================================================
USE [drg];
GO

-- ---- official group catalogue: 839 groups, 二三级 / 一级 weights ----------
IF OBJECT_ID(N'dbo.dict_group_catalog', N'U') IS NULL
CREATE TABLE dbo.dict_group_catalog (
    mdc            varchar(8)     NULL,
    drg_code       varchar(16)    NOT NULL,
    drg_name       nvarchar(200)  NULL,
    drg_attr       nvarchar(60)   NULL,
    drg_type       nvarchar(60)   NULL,
    weight_23      decimal(14,6)  NULL,
    weight_1       decimal(14,6)  NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_group_catalog PRIMARY KEY (drg_code, policy_version)
);
GO

-- ---- QY (ungroupable) pseudo groups -------------------------------------
IF OBJECT_ID(N'dbo.dict_qy_group', N'U') IS NULL
CREATE TABLE dbo.dict_qy_group (
    mdc            varchar(8)   NULL,
    qy_code        varchar(16)  NOT NULL,
    policy_version nvarchar(80)  NOT NULL,
    CONSTRAINT PK_dict_qy_group PRIMARY KEY (qy_code, policy_version)
);
GO

-- ---- ADRG entry conditions (surgical / diagnostic) ----------------------
IF OBJECT_ID(N'dbo.dict_adrg_cond', N'U') IS NULL
CREATE TABLE dbo.dict_adrg_cond (
    mdc            varchar(8)   NULL,
    adrg           varchar(8)   NOT NULL,
    kind           varchar(4)   NOT NULL,      -- op | dx
    code           varchar(40)  NOT NULL,
    name           nvarchar(300) NULL,
    policy_version nvarchar(80)  NOT NULL,
    CONSTRAINT PK_dict_adrg_cond PRIMARY KEY (adrg, kind, code, policy_version)
);
GO

-- ---- ADRG -> DRG (4th digit variants) -----------------------------------
IF OBJECT_ID(N'dbo.dict_drg', N'U') IS NULL
CREATE TABLE dbo.dict_drg (
    adrg           varchar(8)    NOT NULL,
    drg            varchar(16)   NOT NULL,
    drg_name       nvarchar(200) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_drg PRIMARY KEY (drg, policy_version)
);
GO

-- ---- ADRG names ---------------------------------------------------------
IF OBJECT_ID(N'dbo.dict_adrg_name', N'U') IS NULL
CREATE TABLE dbo.dict_adrg_name (
    adrg           varchar(8)    NOT NULL,
    adrg_name      nvarchar(200) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_adrg_name PRIMARY KEY (adrg, policy_version)
);
GO

-- ---- MDCZ (multiple severe trauma) diagnosis list ------------------------
-- A cross-MDC priority rule rather than a plain ADRG: the source workbook stores the
-- diagnosis-group NAME where an ADRG code would normally be. Not yet wired into the
-- engine (needs the clinical severity criteria as well), so it is kept as reference data.
IF OBJECT_ID(N'dbo.dict_mdcz_dx', N'U') IS NULL
CREATE TABLE dbo.dict_mdcz_dx (
    mdc            varchar(8)    NULL,
    grp_name       nvarchar(60)  NOT NULL,
    code           varchar(40)   NOT NULL,
    name           nvarchar(300) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_mdcz_dx PRIMARY KEY (grp_name, code, policy_version)
);
GO

-- ---- main diagnosis -> MDC ---------------------------------------------
-- A diagnosis code may legitimately belong to two MDCs (e.g. C49.503 会阴结缔组织恶性肿瘤 is
-- MDCM for a male and MDCN for a female), so mdc is part of the key.
IF OBJECT_ID(N'dbo.dict_main_dx', N'U') IS NULL
CREATE TABLE dbo.dict_main_dx (
    mdc            varchar(8)    NOT NULL,
    code           varchar(40)   NOT NULL,
    name           nvarchar(300) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_main_dx PRIMARY KEY (code, mdc, policy_version)
);
GO

-- ---- CC / MCC / exclusion lists ----------------------------------------
IF OBJECT_ID(N'dbo.dict_severity', N'U') IS NULL
CREATE TABLE dbo.dict_severity (
    kind           varchar(12)   NOT NULL,     -- cc | mcc | exclude
    code           varchar(40)   NOT NULL,
    name           nvarchar(300) NULL,
    note           nvarchar(300) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_severity PRIMARY KEY (kind, code, policy_version)
);
GO

-- ---- codes that must not be used as main diagnosis / operation ---------
IF OBJECT_ID(N'dbo.dict_no_main', N'U') IS NULL
CREATE TABLE dbo.dict_no_main (
    kind           varchar(4)    NOT NULL,     -- dx | op
    code           varchar(40)   NOT NULL,
    name           nvarchar(300) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_no_main PRIMARY KEY (kind, code, policy_version)
);
GO

-- ---- Chongqing TCM advantage groups (27号文 annex 2) --------------------
IF OBJECT_ID(N'dbo.dict_tcm_group', N'U') IS NULL
CREATE TABLE dbo.dict_tcm_group (
    grp_code       varchar(8)    NOT NULL,
    grp_name       nvarchar(200) NULL,
    order_no       int           NULL,
    days_min       int           NULL,
    op_mode        varchar(12)   NULL,         -- any | combo
    min_ops        int           NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_tcm_group PRIMARY KEY (grp_code, policy_version)
);
GO

IF OBJECT_ID(N'dbo.dict_tcm_group_dx', N'U') IS NULL
CREATE TABLE dbo.dict_tcm_group_dx (
    grp_code       varchar(8)    NOT NULL,
    dx_code        varchar(40)   NOT NULL,
    dx_name        nvarchar(300) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_tcm_group_dx PRIMARY KEY (grp_code, dx_code, policy_version)
);
GO

IF OBJECT_ID(N'dbo.dict_tcm_group_op', N'U') IS NULL
CREATE TABLE dbo.dict_tcm_group_op (
    grp_code       varchar(8)    NOT NULL,
    op_class       varchar(8)    NOT NULL,     -- 0..4
    op_code        varchar(40)   NOT NULL,
    op_name        nvarchar(300) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_tcm_group_op PRIMARY KEY (grp_code, op_code, policy_version)
);
GO

-- ---- fee item attributes (中治率 subjects + TCM operation class) --------
IF OBJECT_ID(N'dbo.dict_cost_item_attr', N'U') IS NULL
CREATE TABLE dbo.dict_cost_item_attr (
    列(cost_no)      varchar(30)   NOT NULL,
    列(cost_name)    nvarchar(200) NULL,
    列(sort_code)    varchar(12)   NULL,
    列(sort_kind)    nvarchar(60)  NULL,
    op_class       varchar(8)    NULL,
    zzl_subject    nvarchar(60)  NULL,
    is_tcm_std     bit           NULL,
    is_tcm_cq4     bit           NULL,
    is_tcm_cost04  bit           NULL,
    is_tcm_item    bit           NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_cost_item_attr PRIMARY KEY (列(cost_no), policy_version)
);
GO

-- ---- disease-case homepage operation code -> TCM operation class --------
IF OBJECT_ID(N'dbo.dict_homepage_op_class', N'U') IS NULL
CREATE TABLE dbo.dict_homepage_op_class (
    op_code        varchar(30)   NOT NULL,
    op_name        nvarchar(200) NULL,
    op_class       varchar(8)    NULL,
    kind           nvarchar(20)  NULL,         -- 中医操作 | 非中医操作 | 待核
    match_mode     nvarchar(30)  NULL,
    evidence       nvarchar(400) NULL,
    cases          int           NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_homepage_op_class PRIMARY KEY (op_code, policy_version)
);
GO

-- ---- characteristic drug -> candidate diagnosis (hint only) ------------
IF OBJECT_ID(N'dbo.dict_drug_hint', N'U') IS NULL
CREATE TABLE dbo.dict_drug_hint (
    pattern        nvarchar(400) NOT NULL,
    dx_prefixes    nvarchar(200) NULL,
    label          nvarchar(100) NULL,
    confidence     nvarchar(10)  NULL,
    note           nvarchar(200) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_dict_drug_hint PRIMARY KEY (pattern, policy_version)
);
GO

-- ---- settlement parameters (rate / coefficient / policy thresholds) -----
IF OBJECT_ID(N'dbo.param_settlement', N'U') IS NULL
CREATE TABLE dbo.param_settlement (
    param_key      varchar(60)   NOT NULL,
    param_value    decimal(18,6) NULL,
    unit           nvarchar(40)  NULL,
    note           nvarchar(300) NULL,
    policy_basis   nvarchar(300) NULL,
    policy_version nvarchar(80)   NOT NULL,
    CONSTRAINT PK_param_settlement PRIMARY KEY (param_key, policy_version)
);
GO
