-- =====================================================================
-- 03  Result layer (drg.result_*).
-- Deliberately DE-IDENTIFIED: patient_name / id_card / person_code from the
-- official return are NOT copied here. The admission number is kept because
-- it is the join key used across every job.
-- Idempotent: tables are only created when absent.
-- =====================================================================
USE [drg];
GO

-- ---- official settlement returns (N back-test cases) -----------------
IF OBJECT_ID(N'dbo.result_settlement_return', N'U') IS NULL
CREATE TABLE dbo.result_settlement_return (
    settle_id        varchar(40)   NOT NULL,
    org_settle_id    varchar(40)   NULL,
    medical_no       varchar(40)   NULL,
    biz_no           varchar(40)   NULL,
    gender           nvarchar(8)   NULL,
    age              int           NULL,
    insurance_type   nvarchar(60)  NULL,
    medical_category nvarchar(60)  NULL,
    office_name      nvarchar(80)  NULL,
    refund_flag      varchar(8)    NULL,
    drg_code         varchar(16)   NULL,
    drg_name         nvarchar(200) NULL,
    weight           decimal(14,6) NULL,
    fee_rate         decimal(14,6) NULL,
    org_coefficient  decimal(14,6) NULL,
    drg_standard     decimal(18,2) NULL,
    settle_date      datetime      NULL,
    in_time          datetime      NULL,
    out_time         datetime      NULL,
    actual_days      int           NULL,
    total_cost       decimal(18,2) NULL,
    deductible       decimal(18,2) NULL,
    first_self_pay   decimal(18,2) NULL,
    account_mutual   decimal(18,2) NULL,
    personal_account decimal(18,2) NULL,
    self_cost_all    decimal(18,2) NULL,
    profit_loss      decimal(18,2) NULL,
    main_diag_code   varchar(40)   NULL,
    main_op_code     varchar(40)   NULL,
    other_diag_codes nvarchar(1000) NULL,
    other_op_codes   nvarchar(1000) NULL,
    list_upload_days int           NULL,
    settle_ym        varchar(10)   NULL,
    source_file      nvarchar(200) NULL,
    source_sheet     nvarchar(120) NULL,
    row_uid          nvarchar(120) NULL,      -- carries the source file name, which is Chinese
    policy_version   nvarchar(80)  NOT NULL,
    CONSTRAINT PK_result_settlement_return PRIMARY KEY (settle_id)
);
GO

-- ---- difference analysis: official vs vendor pre-grouping vs engine -----
IF OBJECT_ID(N'dbo.result_compare_case', N'U') IS NULL
CREATE TABLE dbo.result_compare_case (
    medical_no           varchar(40)   NOT NULL,
    settle_id            varchar(40)   NULL,
    grp_official         varchar(16)   NULL,
    grp_vendor           varchar(16)   NULL,
    grp_engine           varchar(16)   NULL,
    adrg_official        varchar(8)    NULL,
    adrg_engine          varchar(8)    NULL,
    agree_drg            bit           NULL,      -- 4-digit agreement, engine vs official
    agree_adrg           bit           NULL,      -- first 3 digits
    agree_adrg_vendor    bit           NULL,
    path                 varchar(12)   NULL,      -- tcm | standard | none
    official_tcm_group   bit           NULL,
    engine_tcm_group     bit           NULL,
    drg_standard         decimal(18,2) NULL,
    weight               decimal(14,6) NULL,
    total_cost           decimal(18,2) NULL,
    profit_loss          decimal(18,2) NULL,
    actual_days          int           NULL,
    main_diag_code       varchar(40)   NULL,
    main_op_code         varchar(40)   NULL,
    reason               nvarchar(300) NULL,
    policy_version       nvarchar(80)  NOT NULL,
    CONSTRAINT PK_result_compare_case PRIMARY KEY (medical_no, policy_version)
);
GO

-- ---- engine output per case (S11) --------------------------------------
IF OBJECT_ID(N'dbo.result_engine_pred', N'U') IS NULL
CREATE TABLE dbo.result_engine_pred (
    medical_no     varchar(40)   NOT NULL,
    pred_drg       varchar(16)   NULL,
    pred_adrg      varchar(8)    NULL,
    pred_sev       varchar(4)    NULL,
    pred_weight    decimal(14,6) NULL,
    pred_std       decimal(18,2) NULL,
    path           varchar(12)   NULL,
    qy_code        varchar(16)   NULL,
    reason         nvarchar(300) NULL,
    policy_version nvarchar(80)  NOT NULL,
    CONSTRAINT PK_result_engine_pred PRIMARY KEY (medical_no, policy_version)
);
GO

-- ---- coding assistant findings (S14) -----------------------------------
-- surrogate key: the finding text is nvarchar(1000) and cannot be part of a primary key
-- (900-byte index limit), and jobs reload this table wholesale anyway
IF OBJECT_ID(N'dbo.result_coding_finding', N'U') IS NULL
CREATE TABLE dbo.result_coding_finding (
    id             bigint IDENTITY(1,1) PRIMARY KEY,
    medical_no     varchar(40)   NOT NULL,
    [rule]         varchar(8)    NOT NULL,      -- R1..R5
    confidence     nvarchar(10)  NULL,
    finding        nvarchar(1000) NULL,
    basis          nvarchar(600) NULL,
    impact         nvarchar(400) NULL,
    policy_version nvarchar(80)   NOT NULL
);
GO

-- ---- counterfactual TCM-tier loss (S14 section ③) ----------------------
IF OBJECT_ID(N'dbo.result_coding_counterfactual', N'U') IS NULL
CREATE TABLE dbo.result_coding_counterfactual (
    medical_no        varchar(40)   NOT NULL,
    official_drg      varchar(16)   NULL,
    counterfactual_drg varchar(16)  NULL,
    group_name        nvarchar(200) NULL,
    std_official      decimal(18,2) NULL,
    std_counterfactual decimal(18,2) NULL,
    delta             decimal(18,2) NULL,
    policy_version    nvarchar(80)  NOT NULL,
    CONSTRAINT PK_result_coding_counterfactual PRIMARY KEY (medical_no, policy_version)
);
GO

-- ---- in-hospital simulator snapshot (S06), one row per patient per day --
IF OBJECT_ID(N'dbo.result_insim', N'U') IS NULL
CREATE TABLE dbo.result_insim (
    snapshot_date  date          NOT NULL,
    hospital_no    varchar(40)   NOT NULL,
    doctor         nvarchar(80)  NULL,
    doctor_phone   varchar(20)   NULL,   -- 责任医师手机号（sys_doctor，docs/29）
    office         nvarchar(80)  NULL,
    days           int           NULL,
    main_dx        varchar(40)   NULL,
    main_dx_name   nvarchar(300) NULL,
    ops_text       nvarchar(600) NULL,
    op_classes     varchar(40)   NULL,
    tcm_ratio      decimal(10,4) NULL,
    tcm_ratio_note nvarchar(80)  NULL,
    path_basis     nvarchar(80)  NULL,
    line1_group    nvarchar(400) NULL,
    line2_gap      nvarchar(600) NULL,
    line3_cost     nvarchar(300) NULL,
    line4_advice   nvarchar(600) NULL,
    drg_code       varchar(16)   NULL,
    weight         decimal(14,6) NULL,
    std_cost       decimal(18,2) NULL,
    fee_to_date    decimal(18,2) NULL,
    delta          decimal(18,2) NULL,
    policy_version nvarchar(80)  NOT NULL,
    CONSTRAINT PK_result_insim PRIMARY KEY (snapshot_date, hospital_no)
);
GO

-- doctor_phone added later (docs/29: 责任医师=住院医师，手机号代替工号). Idempotent so the
-- column is also back-filled onto databases where result_insim predates it.
IF COL_LENGTH('dbo.result_insim', 'doctor_phone') IS NULL
ALTER TABLE dbo.result_insim ADD doctor_phone varchar(20) NULL;
GO

-- ---- finance reconciliation per case (S07) ------------------------------
IF OBJECT_ID(N'dbo.result_recon_case', N'U') IS NULL
CREATE TABLE dbo.result_recon_case (
    medical_no     varchar(40)   NOT NULL,
    drg_code       varchar(16)   NULL,
    total_cost     decimal(18,2) NULL,
    his_fee        decimal(18,2) NULL,
    his_fee_pos    decimal(18,2) NULL,
    diff           decimal(18,2) NULL,
    diff_pos       decimal(18,2) NULL,
    diff_type      nvarchar(60)  NULL,
    drg_standard   decimal(18,2) NULL,
    profit_loss    decimal(18,2) NULL,
    his_pnl        decimal(18,2) NULL,
    weight         decimal(14,6) NULL,
    fee_rate       decimal(14,6) NULL,
    zzl_ratio      decimal(10,4) NULL,
    policy_version nvarchar(80)  NOT NULL,
    CONSTRAINT PK_result_recon_case PRIMARY KEY (medical_no, policy_version)
);
GO

-- ---- P&L board, three levels unified in one table ----------------------
IF OBJECT_ID(N'dbo.result_pnl', N'U') IS NULL
CREATE TABLE dbo.result_pnl (
    level          varchar(12)   NOT NULL,      -- hospital | drg | office
    period         varchar(10)   NOT NULL,      -- YYYY-MM, or ALL for a full-period roll-up
    key_code       nvarchar(80)  NOT NULL,    -- office level stores the department name
    key_name       nvarchar(200) NULL,
    cases          int           NULL,
    total_cost     decimal(18,2) NULL,
    std_cost       decimal(18,2) NULL,
    profit_loss    decimal(18,2) NULL,
    cmi            decimal(10,4) NULL,
    tcm_share      decimal(10,4) NULL,
    avg_cost       decimal(18,2) NULL,
    avg_pnl        decimal(18,2) NULL,
    is_tcm_group   bit           NULL,
    policy_version nvarchar(80)  NOT NULL,
    CONSTRAINT PK_result_pnl PRIMARY KEY (level, period, key_code, policy_version)
);
GO
