-- Which source does the platform's settlement return actually echo: the HIS diagnosis
-- detail table (源表(diagnosis), multi-row) or the flattened 病案首页 (single slot)?
-- Answers docs/22's open question "平台收码 ≠ 病案首页所存码".
-- Read-only.
WITH pick AS (
    SELECT 列(hospital_no), MAX(列(kind_input)) AS ki
    FROM 源表(diagnosis)
    WHERE 列(kind_input) IN (3, 4)
    GROUP BY 列(hospital_no)
),
maindx AS (
    SELECT d.列(hospital_no), d.列(icd)
    FROM 源表(diagnosis) d
    JOIN pick p ON p.列(hospital_no) = d.列(hospital_no) AND p.ki = d.列(kind_input)
    WHERE d.列(order) = 1 AND d.列(kind_diagnose) = 2
)
SELECT COUNT(1)                                                        AS 例数,
       SUM(CASE WHEN r.main_diag_code = m.列(icd) THEN 1 ELSE 0 END)      AS 与HIS明细主诊断一致,
       SUM(CASE WHEN r.main_diag_code = h.[西医诊断_疾病编码] THEN 1 ELSE 0 END) AS 与病案首页主诊断一致
FROM drg.dbo.result_settlement_return r
LEFT JOIN maindx m ON m.列(hospital_no) = r.medical_no
LEFT JOIN med_record.dbo.源表(case_man) h ON h.[住院号] = r.medical_no
WHERE r.source_sheet = N'3月';
