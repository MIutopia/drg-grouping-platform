-- Drug master vs. Medicare memo: is the indication ("适应症") / contraindication
-- ("禁忌") text actually entered? Read-only analysis query for docs/31 §1.4.
--
-- Run: python src/etl/s30_dx_indication_sample.py --file src/sql/adhoc/09_...sql \
--          --groupby 缺失情况 --save medicine_indication_gap
SELECT
    m.列(cost_no)                        AS 收费编码,
    m.列(cost_name)                      AS 药品名,
    m.列(sort_kind)                      AS 分类,
    cm.列(grade)                         AS 医保等级,
    CASE
        WHEN cm.列(memo) IS NULL OR LEN(LTRIM(RTRIM(cm.列(memo)))) = 0
             THEN N'整条为空'
        WHEN cm.列(memo) NOT LIKE N'%适应症%' AND cm.列(memo) NOT LIKE N'%禁忌%'
             THEN N'无适应症·无禁忌症'
        WHEN cm.列(memo) NOT LIKE N'%适应症%' THEN N'缺适应症'
        WHEN cm.列(memo) NOT LIKE N'%禁忌%'   THEN N'缺禁忌症'
        ELSE N'完整'
    END                                AS 缺失情况,
    cm.列(memo)                          AS 说明原文
FROM      源表(medicine_dict)                 m
LEFT JOIN 源表(cost_medicare_link)    cn ON cn.列(cost_no) = m.列(cost_no)
LEFT JOIN 源表(cost_medicare)            cm ON cm.列(cost_no) = cn.列(medicare_no)
WHERE m.列(mark_open) = '1'
  AND m.列(sort_code) <> '03'
  AND m.列(sort_kind) <> '卫生材'
  AND m.列(pharmacy) = '1080'
  AND m.列(stock_number) <> '0'
  AND (   cm.列(memo) IS NULL
       OR LEN(LTRIM(RTRIM(cm.列(memo)))) = 0
       OR cm.列(memo) NOT LIKE N'%适应症%'
       OR cm.列(memo) NOT LIKE N'%禁忌%' )
ORDER BY 缺失情况, m.列(sort_kind), m.列(cost_name);
