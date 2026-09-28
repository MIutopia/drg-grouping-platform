-- =====================================================================
-- 11  Make the P&L profit basis explicit on result_pnl (test report D5).
--
-- Why: result_pnl.profit_loss blends two conventions - the official per-case field (which
-- the payer zeroes for high/low-rate cases, 某地区医保局〔2025〕26号) and the agreed formula
-- (payment standard - total cost) used for months whose return has no profit column. At
-- aggregate level the blend legitimately breaks "profit_loss = std_cost - total_cost",
-- which left Finance and Medical Affairs unable to explain the same table two ways.
--
-- Three additive columns make it self-describing and machine-checkable:
--   profit_formula = std_cost - total_cost                     (formula value, exact)
--   profit_adjust  = profit_loss - profit_formula              (official adjustment, exact)
--   profit_basis   = which source(s) fed this row              (provenance, for humans)
-- Invariants (zero drift expected, usable as a test assertion):
--   profit_formula = std_cost - total_cost
--   profit_loss    = profit_formula + profit_adjust
--
-- Idempotent: each column is added only when missing. Existing rows are backfilled by
-- re-running s08 (board export) + s18 --load.
-- =====================================================================
USE [drg];
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID(N'dbo.result_pnl') AND name = N'profit_formula')
ALTER TABLE dbo.result_pnl ADD profit_formula decimal(18,2) NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID(N'dbo.result_pnl') AND name = N'profit_adjust')
ALTER TABLE dbo.result_pnl ADD profit_adjust decimal(18,2) NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID(N'dbo.result_pnl') AND name = N'profit_basis')
ALTER TABLE dbo.result_pnl ADD profit_basis nvarchar(60) NULL;
GO
