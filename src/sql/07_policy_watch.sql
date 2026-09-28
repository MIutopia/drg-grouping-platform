-- =====================================================================
-- 07  Policy document watch (drg.dict_policy_doc / dict_policy_fact).
--
-- Why this exists: docs/24 step 1 assumed a PUSH channel - the bureau issues
-- a document, the hospital logs it, someone reads it. There is no such channel.
-- The bureau publishes a pile of documents and the hospital has to go and read
-- them, so "radar + incoming-document register" cannot be built on an inbound
-- feed that does not exist.
--
-- The substitute is a PULL channel that is reproducible: the files are treated
-- as a versioned data source. Drop a new file into 原始资料/, run s21, and the job
-- fingerprints it, extracts the policy facts, and diffs them against what the
-- system currently uses. The register becomes a by-product of the scan, so it
-- needs no one to maintain a log by hand.
-- Idempotent: tables are only created when absent.
-- =====================================================================
USE [drg];
GO

-- ---- document register: one row per policy file per scan ---------------
IF OBJECT_ID(N'dbo.dict_policy_doc', N'U') IS NULL
CREATE TABLE dbo.dict_policy_doc (
    id             bigint IDENTITY(1,1) PRIMARY KEY,
    -- doc_key is derived from the Chinese file name, so it MUST be nvarchar: under
    -- Chinese_PRC_CI_AS a Chinese character is 2 bytes and overflows a narrow varchar
    doc_key        nvarchar(120) NOT NULL,      -- stable key derived from the file name
    file_name      nvarchar(200) NOT NULL,
    rel_path       nvarchar(300) NOT NULL,
    ext            varchar(8)    NULL,
    sha256         varchar(64)   NULL,          -- change detection on the bytes themselves
    size_bytes     bigint        NULL,
    pages          int           NULL,          -- PDF pages
    tables_n       int           NULL,          -- docx tables / xlsx sheets
    rows_n         bigint        NULL,          -- extracted rows
    doc_no         nvarchar(80)  NULL,          -- 文号, e.g. 某地区医保局〔2025〕27号
    effective_date nvarchar(40)  NULL,
    category       nvarchar(8)   NULL,          -- ①..⑥ per docs/24
    captured_at    datetime      NOT NULL,
    policy_version nvarchar(80)  NOT NULL
);
GO

-- ---- facts extracted from the documents --------------------------------
-- Stored per scan rather than only printed: the useful question six months later
-- is "what did this document say then", not "what does the current dictionary say".
IF OBJECT_ID(N'dbo.dict_policy_fact', N'U') IS NULL
CREATE TABLE dbo.dict_policy_fact (
    id             bigint IDENTITY(1,1) PRIMARY KEY,
    doc_key        nvarchar(120) NOT NULL,      -- Chinese file-derived key, must be nvarchar
    fact_key       varchar(40)   NOT NULL,
    fact_label     nvarchar(120) NULL,
    fact_value     nvarchar(120) NULL,
    captured_at    datetime      NOT NULL,
    policy_version nvarchar(80)  NOT NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_dict_policy_fact_key')
CREATE INDEX IX_dict_policy_fact_key ON dbo.dict_policy_fact(fact_key, captured_at);
GO
