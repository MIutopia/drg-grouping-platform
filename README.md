# DRG 分组与结算工作台（通用版）

医保 DRG 付费场景下的一套自建工具链：**分组引擎 + 数据治理作业 + 多角色工作台 + 运维监控**。
本仓库是**去标识化的通用版本**：不含任何具体医院、人员、真实数据或院内网络信息，源库表名按
"现场 HIS 契约"保留并在配置中说明；本地政策差异（分组目录、权重、费率、机构系数）都以
**可替换的本地目录文件**形式提供，不绑定任何地区。

## 能力概览

| 层 | 内容 |
|---|---|
| 分组引擎 | CHS-DRG 2.0 规则 + 本地优势病组叠加；主诊断/主操作 → MDC → ADRG → CC/MCC 严重程度 → DRG；含 MCC/CC 排除表、QY 未入组判定 |
| 数据作业 | 结算返回导入、字典抽取、差异比对、盈亏看板、对账、编码助手、在院模拟、回归门槛（`src/etl/s*.py`） |
| 工作台 | FastAPI 后端（登录/鉴权/权限矩阵/审计）+ Vue3 前端；医师 / 财务 / 收费 / 信息科 / 医务科五类视图 |
| 运维 | 新鲜度与告警、受控 DDL（sha256 留痕）、凭据密文化、回滚演练 |

## 快速开始

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r src/etl/requirements.txt
pip install -r src/web/backend/requirements.txt
# 1) 配置连接档案（模板 → 实际，勿提交凭据）
copy src/config/local.example.json src/config/local.json
# 2) 建库并装载（需要有可用的 SQL Server 与源库；见 docs/23）
$env:DRG_ALLOW_DDL=1; python src/etl/s18_create_drg_db.py --create
python src/etl/s18_create_drg_db.py --load
# 3) 分组回测与回归门槛
python src/etl/s11_adrg.py
python src/etl/s20_regress.py --dry
# 4) 工作台
python src/web/backend/seed_users.py      # 需先注入 DRG_INIT_PWD
uvicorn app:app --host 127.0.0.1 --port 8000 --app-dir src/web/backend
```

## 目录

```
src/sql/      建库与迁移脚本（幂等、受控执行）
src/etl/      作业层与运行时模块（dbio/ddlio/drg_engine/tcm_ratio…）
src/web/      FastAPI 后端 + Vue3 前端
src/config/   settings/secure（凭据密文化）+ 字典产物 dict/ + 规则白名单
src/run/      计划任务入口（在院快照、防火墙）
docs/          设计与作业文档（已通用化）
```

## 源库（HIS）契约：表名与列名都走配置

代码里不写任何现场物理名，只写自解释令牌：

* 表名 `源表(key)` —— 物理名在 `src/config/source_schema.json`
* 列名 `列(key)`（SQL）/ `src_col(key)`（pandas 列访问）—— 物理名在
  `src/config/source_columns.json`

**部署时把两份配置的每个值改成本现场的真实名**，`dbio` 会在执行前展开——这是全项目唯一的
展开点；缺键会明确报错，不会静默拼出空名。**查询结果的列名仍是物理列名**，因此 CSV 产物与
装载契约不变。`python src/etl/s00_env_check.py` 会自检"代码里是否又写回了物理名"。

## 数据与合规

- **本仓库不含真实数据**：产出目录 `src/out/`、原始资料目录均不入库（见 `.gitignore`）。
- 运行期凭据只经环境变量或本地配置文件注入，仓库内仅保留 `.example.json` 模板。
- 内置合规闸门：`output_guard` 对产出做禁止项校验；编码助手只给"提示"不写码；受控 DDL 需
  `DRG_ALLOW_DDL=1` 且仅执行 `src/sql/` 下脚本并记录 sha256。
- 落地到具体机构时，请替换：分组目录/权重/费率、机构系数、源库表名字典、本地政策阈值。

## 许可

未指定许可证前，默认保留所有权利（如需开源请补充 LICENSE）。
