# 《硅基涌现》随身通信网关插件开发全景与避坑指南

本文档完整复盘将 **合宙 Air780EPV 智能随身通信网关** 接入个人数字资产生命系统 **《硅基涌现》（Agent_V3）** 的全过程，沉淀架构方案、实现细节、真机实测踩坑记录与关键工程经验。

---

## 1. 背景与业务目标

### 1.1 为什么要把蜂窝网关接入《硅基涌现》？
* **痛点**：手机短信（银行月度账单、官方办事通知、充值扣费、电子发票代码）具有极高记忆与证明价值，但分散在物理手机卡内，极易随换手机而丢失，且无法被用户的 AI 知识库检索；
* **破局**：Air780EPV 模组通过 4G 物理信令（0 字节/0 扣费）接收短信，电脑后台 `gateway_hub.py` 宽带代推并抽象为极轻量 IPC 接口。
* **业务闭环**：通过《硅基涌现》插件体系，将蜂窝通信记录无缝转换为《硅基涌现》的**个人数字资产**，并在 Web 端提供可视化的**【随身通信网关】**独立工作区。

---

## 2. 架构设计：Bridge-and-Surface 协同模型

《硅基涌现》采用严苛的 **Plugin Platform Architecture v4.0** 规范。由于宿主安全策略，插件 Worker 被机械性禁止使用网络（`node:net`, `fetch`），因此必须采用 **“外围桥接 + 存储中转 + 沙箱渲染”** 的分工模型：

```text
┌─────────────────────────────────────────────────────────────┐
│ 宿主物理环境 (PC / NAS)                                     │
│                                                             │
│   Air780EPV 模组 ──(COM8)──► gateway_hub.py (127.0.0.1:17800)│
│                                      │ (TCP NDJSON)         │
│                                      ▼                      │
│                          gateway_bridge_silicon.py          │
│                                      │                      │
│                                      ▼                      │
│                  .runtime/silicon_gateway_snapshot.json     │
└──────────────────────────────────────┬──────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 《硅基涌现》系统 (Agent_V3 / Node.js 24)                     │
│                                                             │
│   POST /api/plugin-platform/packages/inspect                │
│       │ (安装并启用 cellular-gateway.se-plugin)             │
│       ▼                                                     │
│   Worker Plugin Runtime (受控线程沙箱)                      │
│     - pluginId: "com.smartgateway.cellular"                 │
│     - 消费 plugin.storage (config / cache)                  │
│     - 响应 workspace.command (refresh, fetch_otp)           │
│     - 格式化输出 plugin.workspace.v1 Surface DTO            │
│       ▼                                                     │
│   前端页面 (PluginWorkspaceSurface)                          │
│     - 侧边栏常驻【随身通信网关】入口 (plug 图标)            │
│     - 实时渲染信号 CSQ、温度、供电、0流量防偷跑及最新验证码 │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 插件实现细节与工程规范

### 3.1 标准 Manifest 配置 (`manifest.json`)
```json
{
  "pluginId": "com.smartgateway.cellular",
  "name": "Air780EPV 随身通信网关",
  "version": "1.0.0",
  "apiVersion": "1.0.0",
  "hostCompatibility": ">=0.2.0 <0.3.0",
  "entry": "index.mjs",
  "permissions": [
    "plugin.storage"
  ],
  "contributions": [
    {
      "contributionId": "dashboard",
      "kind": "workspace",
      "target": "workbench.main",
      "label": "随身通信网关",
      "icon": "plug",
      "commands": [
        {
          "commandId": "refresh",
          "label": "刷新状态"
        },
        {
          "commandId": "fetch_otp",
          "label": "获取最新验证码"
        }
      ]
    }
  ]
}
```

### 3.2 Worker 执行逻辑 (`index.mjs`)
* 消费 `@silicon/plugin-sdk` 的 `storage.cache`；
* 格式化输出符合 `plugin.workspace.v1` 契约的 JSON DTO；
* 状态未就绪时输出友好提示引导用户；快照就绪时渲染富文本看板与高亮验证码。

### 3.3 自动化打包工具 (`package_plugin.py`)
* 将源码打包为标准的 `.se-plugin`（ZIP 格式，Deflated 压缩）；
* 自动计算 SHA-256 哈希值；当前产物位于 `dist/cellular-gateway.se-plugin`（3.0 KB）。

---

## 4. 踩坑记录与关键复盘（重点经验）

在首次真机安装到《硅基涌现》时，遇到了两个典型错误，深入源码排查后得到以下关键认知：

### 坑 1：`plugin_package_invalid`（本地插件包未通过检查）
* **报错现象**：上传插件包后，前端弹窗提示：  
  `本地插件包未通过检查，入口未执行。现有插件和资料不受影响；请选择另一个包。`
* **根因溯源**：
  查阅《硅基涌现》`src/modules/plugin-platform/manifest-v1.js`，发现其对 Manifest 字段做的是**硬编码死白名单校验**：
  1. **图标名称超限**：代码定义 `const ALLOWED_ICONS = new Set(["file", "folder", "layout", "list", "plug", "puzzle", "search", "settings", "table", "tag", "terminal", "workflow"])`，最初插件写了 `"icon": "broadcast"`，直接被正则拒绝；
  2. **权限声明超限**：代码定义 `const ALLOWED_PERMISSIONS = new Set(["plugin.storage"])`，最初插件写了 `["plugin.storage", "plugin.log"]`，包含未在当前生产白名单中的 `plugin.log` 被直接驳回。
* **破局方案**：
  * 将 `icon` 修正为合规内置图标 `"plug"`；
  * 将 `permissions` 收敛为单一合规权限 `["plugin.storage"]`；
  * 重新打包后该错误彻底消除。

---

### 坑 2：`plugin_candidate_not_selected`（生产运行适配器尚未选定）
* **报错现象**：安装或启用时提示：  
  `生产运行适配器尚未选定，当前插件操作未执行。其他插件和资料不受影响；请等待选型后重试。`
* **根因溯源**：
  查阅 `candidate-gate.js` 与 `server/composition/plugin-platform-production.mjs`，这是《硅基涌现》系统层的安全门禁：
  1. **PostgreSQL 状态硬绑定**：源码中当 `!hasPostgres` 时，系统将 `candidateStatus` 强制置为 `{ packageReader: "not-selected", workerRuntime: "not-selected" }`。若《硅基涌现》在没有 PostgreSQL 支持的开发纯前端/内存模式下运行，任何第三方包操作都会被拦截；
  2. **ADR-028 生产级防护锁**：在第三方本地扩展包获得正式生产验收前，系统采用 fail-closed 原则阻止不受信 Worker 启动。
* **破局方案**：
  * 确保《硅基涌现》在具备 PostgreSQL 数据库与完整 storageRoot 的正式生产模式下启动；
  * 针对免安装需求，亦可走原生内置插件路径（`builtin-plugin-catalog.js`）。

---

### 坑 3：Worker 线程沙箱的网络封锁
* **报错现象**：如果在 `index.mjs` 中试图直接 `import net from "node:net"` 连接 `127.0.0.1:17800`，测试和运行均会被直接抛出致命异常。
* **根因溯源**：
  《硅基涌现》的 `plugin-worker-bootstrap.mjs` 在加载 Worker 时注入了严格的 Module Loader Hook，只允许加载 `@silicon/plugin-sdk` 和相对 `.mjs`，`globalThis.fetch` 与 `globalThis.WebSocket` 也被重置为 `undefined`。
* **破局方案**：
  彻底放弃在 Worker 内连网络的念头。网络连接由宿主侧守护程序（`gateway_bridge_silicon.py`）完成，通过安全存储（`storage.cache`）作为桥梁进行数据交换。

---

## 5. 验收成果与真机实测证据

### 5.1 契约测试通过 (test_silicon_plugin.py)
* **Manifest 校验**：100% 吻合 `pluginId`, `apiVersion: 1.0.0`, `hostCompatibility: >=0.2.0 <0.3.0`；
* **代码安全扫描**：无任何 `node:net`, `node:fs`, `node:child_process`, `process.env` 等 10 大违规调用；
* **Surface 渲染校验**：`plugin.workspace.v1` 输出格式严密符合前端只读规范。

### 5.2 真实界面渲染验证 (真机截屏)
在《硅基涌现》正式环境中成功激活并展示：
* 侧边栏显示【随身通信网关】导航标签；
* 工作区成功挂载网关 Surface，显示：
  * 状态标识条：`网关未连接 / 等待同步`（初次进入等待快照）；
  * 引导说明：`尚未接收到 Air780EPV 智能网关数据快照... 网关默认运行在 0 流量纯信令保号模式下...`；
  * 操作控件：`[刷新状态]` 交互按钮。
* 实测证据截图存档：`evidence/Screenshot_2026-09-10_15-34-47.png`。

---

## 6. 后续演进建议

1. **守护同步自启**：将 `gateway_bridge_silicon.py` 注册为 Windows 开机自启服务（或与 NAS Docker 容器绑定），实现网关数据 24 小时全天候自动投递；
2. **短信深度资产化**：后续可编写针对《硅基涌现》`AssetService` 的适配器，在新短信到达瞬间直接在本地 PostgreSQL 创建一条带标签的通信资产，全面接入统一搜索与右侧 AI 助手。
