-- Same scope as 09, but WITHOUT the "missing-only" filter, so the result carries the
-- denominator needed to state a completion rate. Read-only.
--
-- Run: python src/etl/s30_dx_indication_sample.py --file src/sql/adhoc/10_...sql \
--          --groupby 缺失情况 --save medicine_indication_coverage
WITH base AS (
    SELECT
        m.列(cost_no)  AS 收费编码,
        m.列(cost_name) AS 药品名,
        cm.列(memo)     AS 说明原文,
        CASE
            WHEN cm.列(memo) IS NULL OR LEN(LTRIM(RTRIM(cm.列(memo)))) = 0
                 THEN N'整条为空'
            WHEN cm.列(memo) NOT LIKE N'%适应症%' AND cm.列(memo) NOT LIKE N'%禁忌%'
                 THEN N'无适应症·无禁忌症'
            WHEN cm.列(memo) NOT LIKE N'%适应症%' THEN N'缺适应症'
            WHEN cm.列(memo) NOT LIKE N'%禁忌%'   THEN N'缺禁忌症'
            ELSE N'完整'
        END AS 缺失情况
    FROM      源表(medicine_dict)              m
    LEFT JOIN 源表(cost_medicare_link) cn ON cn.列(cost_no) = m.列(cost_no)
    LEFT JOIN 源表(cost_medicare)         cm ON cm.列(cost_no) = cn.列(medicare_no)
    WHERE m.列(mark_open) = '1'
      AND m.列(sort_code) <> '03'
      AND m.列(sort_kind) <> '卫生材'
      AND m.列(pharmacy) = '1080'
      AND m.列(stock_number) <> '0'
)
SELECT 缺失情况, COUNT(1) AS 药品数
FROM base
GROUP BY 缺失情况
ORDER BY COUNT(1) DESC;
