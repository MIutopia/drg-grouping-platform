# 30 · DRG 多角色工作台 — IIS 部署配置

> 适用：把「DRG 多角色工作台」从开发态（Vite dev + uvicorn）切到 **Windows Server + IIS** 生产部署，
> 供院内多终端（医务科、信息科、财务/收费、医师）通过内网浏览器访问。
> 配套平台方案见 `docs/29-多角色工作台平台方案.md`。

---

## 1. 目标拓扑

```
院内终端 (192.0.2.0.x / 198.51.100.0.x)
        │  https://192.0.2.0.9  (仅内网段)
        ▼
   ┌───────────── IIS (站点 DRG工作台) ─────────────┐
   │  • 静态托管 dist（前端，hash 路由）             │
   │  • URL Rewrite + ARR 反向代理 /api/* ─────────┐ │
   └──────────────────────────────────────────────┼─┘
                                                   ▼
                                     uvicorn :8000 (本机 127.0.0.1)
                                                   │ pymssql
                                                   ▼
                                    SQL Server 192.0.2.10:1433 (drg 库)
                                    SQL Server 192.0.2.11:1433 (数据字典)
```

要点：
- **前端只发静态文件**，不再需要 Vite dev 服务器（开发态的 `server.host:true` 与之无关）。
- 浏览器所有 `/api` 请求与页面**同源**（同 host:port），因此**不会触发 CORS**；`DRG_WEB_ORIGIN_REGEX` 在生产反代模式下可留空。
- 后端只监听 `127.0.0.1:8000`，由 IIS 本地转发，**不对外暴露 8000 端口**。
- 真正的网络层访问控制交给 **IIS「IP 和域限制」+ Windows 防火墙**，只放行院内网段（见 §8、§9）。

---

## 2. 前置条件

**服务器（Windows Server 2016/2019/2022，建议专机或虚拟机）**
- 已装 IIS（「Web 服务器 (IIS)」角色）。
- IIS 子功能需勾选：
  - 常见 HTTP 功能：**静态内容**、默认文档、目录浏览（可关）、HTTP 错误。
  - 应用程序开发：**WebSocket**（如未用到可不选，本平台用轮询）。
  - 安全性：**IP 和域限制**（用于内网段白名单）。
- 安装 **URL Rewrite 2.1** 与 **Application Request Routing 3.0 (ARR)**（微软官方独立安装包）。
- 安装 **Python 3.10+**（与开发环境一致的版本），并建议建虚拟环境。
- 到 SQL Server `192.0.2.10:1433`、`192.0.2.11:1433` 的网络可达（防火墙放行业务库端口）。

> 备选：若不想单独把后端注册成 Windows 服务，可用 **HTTPPlatformHandler** 由 IIS 直接宿主 uvicorn（见 §13）。

---

## 3. 目录与端口规划

| 项 | 值 |
|---|---|
| 前端构建产物 | `E:\DRG\src\web\frontend\dist` |
| 后端目录 | `E:\DRG\src\web\backend` |
| 后端配置/凭据 | `E:\DRG\src\config\settings.py`、`config\local.json` |
| IIS 站点物理路径 | `E:\DRG\src\web\frontend\dist` |
| 站点绑定 | `https://192.0.2.0.9:443`（证书：院内/内网 CA） |
| 后端监听 | `127.0.0.1:8000`（仅本机） |
| Windows 服务名 | `DRGWebBackend` |

---

## 4. 步骤一：构建前端

在**前端构建机或服务器**上（需 Node.js 18+）：

```powershell
cd E:\DRG\src\web\frontend
npm ci                 # 或 npm install
npm run build          # 产出 dist/（已在 vite.config 中 base 默认相对路径）
```

校验：`dist\index.html` 与 `dist\assets\*.js` 存在。

> 前端采用 **hash 路由**（`#/manager`、`#/ops` …），所有路由都在 `#` 之后，
> 因此 IIS 无需为 SPA 做服务端重写（即便写了 `index.html` 回退也无害）。
> 前端 API 调用均为相对路径 `/api/...`，由 §6 的反代规则转发。

---

## 5. 步骤二：后端部署为 Windows 服务

### 5.1 建虚拟环境并装依赖

```powershell
cd E:\DRG\src\web\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python seed_users.py     # 首次部署建账号（**须先注入 DRG_INIT_PWD**，否则拒绝运行）
```

### 5.2 注册为 Windows 服务（NSSM）

下载 [NSSM](https://nssm.cc/)（Non-Sucking Service Manager），将 `nssm.exe` 放入 `PATH`。

```powershell
nssm install DRGWebBackend "E:\DRG\src\web\backend\.venv\Scripts\python.exe" `
  "-m uvicorn app:app --host 127.0.0.1 --port 8000"
nssm set DRGWebBackend AppDirectory "E:\DRG\src\web\backend"
# 生产必须设置签名密钥（否则每次重启随机生成，旧令牌失效并告警）
nssm set DRGWebBackend AppEnvironmentExtra DRG_WEB_SECRET="<32+位随机串>"
# 可选：登录失败上限/窗口
nssm set DRGWebBackend AppEnvironmentExtra DRG_LOGIN_MAX="5"
nssm set DRGWebBackend AppEnvironmentExtra DRG_LOGIN_WINDOW="600"
# 若数据库凭据需经 env 覆盖（否则读 config/settings.py + local.json）
# nssm set DRGWebBackend AppEnvironmentExtra DRG_SBO_PASSWORD="******"
nssm set DRGWebBackend DisplayName "DRG 工作台后端"
nssm set DRGWebBackend Description "FastAPI + uvicorn，仅本机 127.0.0.1:8000"
nssm start DRGWebBackend
```

验证：

```powershell
curl http://127.0.0.1:8000/api/health        # 期望 {"ok":true,...}
nssm status DRGWebBackend                      # SERVICE_RUNNING
```

> `AppEnvironmentExtra` 可多次 set，每行一个 `KEY=VALUE`；NSSM 会注入为进程环境变量。
> 后端 DB 连接档案在 `config/settings.py`（`sbo` → `192.0.2.10:1433` / `drg` 库，`dict` → `192.0.2.11`），
> 口令建议用 `secure.py` 密文存储，勿明文（详见 `settings.py` 注释）。

---

## 6. 步骤三：IIS 站点 + web.config

### 6.1 建站点
IIS 管理器 → 站点 → 添加网站：
- 站点名称：`DRG工作台`
- 物理路径：`E:\DRG\src\web\frontend\dist`
- 端口：先 80 或 443（§7 配 HTTPS）；主机名可留空（按 IP 访问）或填 `192.0.2.0.9`。
- 应用程序池：`.NET CLR 版本 = 无托管代码`（纯静态 + 反代，无需 .NET）。

### 6.2 启用 ARR 反向代理
IIS 管理器 → 服务器节点 → **Application Request Routing Cache** → 右侧 **Server Proxy Settings** → 勾选 **Enable proxy**，勾选「Reverse rewrite host in response headers」，确定。
（等价命令：`%windir%\system32\inetsrv\appcmd set config -section:system.webServer/proxy -enabled:true -commitpath:apphost`）

### 6.3 站点 `web.config`

把以下文件放到 `E:\DRG\src\web\frontend\dist\web.config`（随构建产物一起部署；如担心被 `npm run build` 覆盖，可在 `dist` 之外单独放一份并手动合并）：

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <defaultDocument>
      <files><add value="index.html" /></files>
    </defaultDocument>

    <rewrite>
      <rules>
        <!-- 1) 反代 /api/* 到本机 uvicorn；必须放在最前且 stopProcessing -->
        <rule name="APIProxy" stopProcessing="true">
          <match url="^api/(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:8000/api/{R:1}" />
        </rule>
        <!-- 2) SPA 回退（hash 路由其实不需要，但保留无害） -->
        <rule name="SPA" stopProcessing="true">
          <match url=".*" />
          <conditions logicalGrouping="Or">
            <add input="{REQUEST_FILENAME}" matchType="IsFile" negate="true" />
          </conditions>
          <action type="Rewrite" url="/index.html" />
        </rule>
      </rules>
    </rewrite>

    <!-- 3) 仅放行院内网段（见 §8 说明：需安装「IP 和域限制」） -->
    <security>
      <ipSecurity allowUnlisted="false">
        <add allowed="true" ipAddress="192.0.2.0.0" subnetMask="255.255.255.0" />
        <add allowed="true" ipAddress="198.51.100.0.0" subnetMask="255.255.255.0" />
        <add allowed="true" ipAddress="127.0.0.1" subnetMask="255.255.255.255" />
      </ipSecurity>
    </security>
  </system.webServer>
</configuration>
```

> 若 `dist` 每次 `npm run build` 被清空，建议把 `web.config` 与静态托管解耦：
> 在 IIS 里把站点物理路径指到 `dist`，但把 `web.config` 放在上层目录或单独应用（避免被构建覆盖），
> 或在 CI/部署脚本末步 `copy web.config.dist dist\web.config`。

---

## 7. 步骤四：HTTPS 与绑定

- 在 IIS 站点「绑定」中添加 `https`，选择院内/内网 CA 签发的证书，端口 `443`，IP 地址选 `192.0.2.0.9`（**不要选 0.0.0.0 全绑**，配合 §8/§9 只暴露内网）。
- 如需强制 HTTPS：URL Rewrite 增加一条「仅 443 进站」重定向规则（可选）。
- 前端 `vite.config` 的 `base` 默认相对路径，HTTPS 下资源自动走 `https://`，无需改代码。

---

## 8. 步骤五：IP 访问限制（内网段白名单）

`web.config` 中 `<ipSecurity allowUnlisted="false">` 即「默认拒绝，仅允许清单」：
- `192.0.2.0/24`、`198.51.100.0/24`：医务科确认的院内内网段（按 `docs/29` 与前期确认）。
- `127.0.0.1`：本机（IIS 反代、健康检查用）。
- 如需临时放运维机，追加一条 `allowed="true"` 即可。

> 这是**网络层**访问控制，比 CORS 才是真正有效的「地址域可访问性」闸门。
> 前置条件：IIS 已装「IP 和域限制」角色；若未装，`<ipSecurity>` 节点会导致 500.19。

---

## 9. 步骤六：Windows 防火墙

```powershell
# 仅对内网段放行 443（前端），8000 只本机无需入站规则
New-NetFirewallRule -DisplayName "DRG IIS 443 (internal)" -Direction Inbound `
  -Protocol TCP -LocalPort 443 -RemoteAddress 192.0.2.0/24,198.51.100.0/24 -Action Allow -Profile Any
# 后端 8000 仅本机：显式禁止外部入站（默认已拦，列出以明志）
New-NetFirewallRule -DisplayName "DRG block 8000 external" -Direction Inbound `
  -Protocol TCP -LocalPort 8000 -RemoteAddress 0.0.0.0/0 -Action Block -Profile Any
```

另需确认：**应用服务器 → SQL Server `192.0.2.10:1433` / `192.0.2.11:1433`** 的防火墙双向放行业务库端口（由数据库侧或网段策略保障）。

---

## 10. 生产环境变量清单（后端服务）

| 变量 | 必填 | 说明 |
|---|---|---|
| `DRG_WEB_SECRET` | **是** | 令牌签名密钥，≥32 位随机串；不设置则每次重启随机生成并告警 |
| `DRG_WEB_ORIGINS` | 否 | 同源反代下可留空；若直连后端才需填站点源 `https://192.0.2.0.9` |
| `DRG_WEB_ORIGIN_REGEX` | 否 | 生产反代模式留空即可（同源不发 CORS 预检） |
| `DRG_LOGIN_MAX` / `DRG_LOGIN_WINDOW` | 否 | 登录失败限速，默认 5 / 600s |
| `DRG_SBO_*` / `DRG_DICT_*` | 否 | DB 连接覆盖（host/port/user/password/database）；缺省读 `config/settings.py` + `local.json` |

---

## 11. 验证清单

从院内终端（如 `192.0.2.0.5`）浏览器访问 `https://192.0.2.0.9`：

1. 首屏加载「DRG 工作台」、无控制台 CORS 报错（同源，理应无）。
2. `/api/health` 经反代可达（页面登录即走 `/api/login`）。
3. 用各角色账号登录，确认侧边栏与权限：
   - 医务科：`医务科看板` + `后台管理`（结算清单上传、白名单审议）。
   - 信息科：`控制台` + `后台管理`（作业触发）。
   - 财务/收费：`财务工作台`；医师：`我的患者`。
4. 越权校验：信息科账号直接调 `POST /api/manager/whitelist` 应返回 **403**。
5. 外网/非内网段 IP 访问被 IIS「IP 和域限制」拒绝（非 403 应用层，而是连接被拦）。

---

## 12. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 页面能开但登录报 CORS / 网络错误 | 反代未生效，`/api` 没转发 | 查 ARR proxy 是否 Enable；查 `web.config` 是否被构建覆盖；`curl 127.0.0.1:8000/api/health` 验证后端 |
| 500.19 站点起不来 | `<ipSecurity>` 缺「IP 和域限制」角色 | 服务器管理器添加该 IIS 安全子功能 |
| `/api` 返回 404 | rewrite 规则顺序/正则错 | 确保 `APIProxy` 在最前且 `stopProcessing`；`match url="^api/(.*)"` |
| 登录 401 但密码对的 | `DRG_WEB_SECRET` 与历史令牌不一致（重启后密钥变了） | 服务环境变量固定 `DRG_WEB_SECRET`，用户重新登录 |
| 页面空白/资源 404 | `dist` 未完整构建或物理路径指错 | 核对 `dist\index.html` 与 IIS 物理路径 |
| 访问超时（非拒绝） | 防火墙拦 443 或网段不匹配 | 核对 §9 规则与终端所在网段 |
| 后端连不上 SQL Server | 业务库端口/凭据/路由 | `python -c "import db; print(db.describe())"` 在服务器本地测连通 |

---

## 13. 备选：HTTPPlatformHandler 直宿主（免单独 Windows 服务）

不想用 NSSM/服务，可让 IIS 直接拉起 uvicorn：

1. 安装 **HTTPPlatformHandler**（IIS 官方模块）。
2. 站点 `web.config` 改用：

```xml
<system.webServer>
  <httpPlatform stdoutLogEnabled="true" stdoutLogFile="E:\DRG\src\run\wfastcgi.log"
    processPath="E:\DRG\src\web\backend\.venv\Scripts\python.exe"
    arguments="-m uvicorn app:app --host 127.0.0.1 --port %HTTP_PLATFORM_PORT%"
    startupTimeLimit="60">
    <environmentVariables>
      <environmentVariable name="DRG_WEB_SECRET" value="<32+位随机串>" />
    </environmentVariables>
  </httpPlatform>
</system.webServer>
```

此时无需 NSSM，IIS 负责进程生命周期；其余静态托管、反代、IP 限制同前。
注意：`httpPlatform` 与 `rewrite` 的 `/api` 反代可并存（HTTPPlatformHandler 处理动态，静态文件仍由 IIS 直接返回）。

---

## 14. 上线检查单（安全 / 等保）

- [ ] `DRG_WEB_SECRET` 已固定且足够随机（不在代码/日志泄露）。
- [ ] 数据库口令经 `secure.py` 密文存储，非明文（见 `config/settings.py` 注释）。
- [ ] 站点仅绑内网 IP（192.0.2.0.9），HTTPS 启用，证书有效。
- [ ] IIS「IP 和域限制」默认拒绝，仅放行院内网段。
- [ ] Windows 防火墙仅开放 443（入站），8000 不外暴露。
- [ ] 后端 8000 仅监听 `127.0.0.1`。
- [ ] 角色权限已用越权用例验证（信息科 ≠ 医务科 ≠ 财务）。
- [ ] 审计表 `sys_audit_log` 正常写入（登录、越权、白名单增删审）。
- [ ] 登录失败限速生效（`DRG_LOGIN_MAX`）。
- [ ] 部署文档与 `web.config` 纳入版本管理（凭据除外）。
