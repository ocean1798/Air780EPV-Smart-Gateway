# 《硅基涌现》物理蜂窝硬件接入与随身网关插件联动需求说明书 (RFC)

本文档面向 **《硅基涌现》（Agent_V3）平台架构师与核心开发团队**，旨在说明将合宙 Air780EPV 智能随身蜂窝通信网关深度接入《硅基涌现》的技术背景、架构瓶颈、推荐实现方案及硬件交互协议规范。

---

## 1. 需求背景与业务价值

* **业务目标**：
  用户将合宙 4G 蜂窝通信模组（Air780EPV）插入家庭 NAS（飞牛 EVO4 / fnOS）的 USB 接口后，《硅基涌现》可直接作为一个全天候在线的家庭通信中枢，实现 0 流量保号短信/验证码捕获、重要账单与办事通知的数字资产化沉淀，并在 Web 端【随身通信网关】工作区实时直观呈现。
* **当前进展**：
  * 通信网关插件（`com.smartgateway.cellular` v1.0.0）已按照 Plugin Platform Architecture v4.0 规范打包为 `.se-plugin` 标准归档；
  * 在飞牛 EVO4 的 Docker 生产环境下，插件已成功上传、校验通过，并在侧边栏顺利挂载激活（`plugin.workspace.v1` 界面已正常显示，真机证据存档于 `evidence/Screenshot_2026-09-10_15-34-47.png`）。
* **当前卡点**：
  * 插件界面当前显示为黄色状态：`网关未连接 / 等待同步`；
  * 受限于当前 Plugin Platform 的 Worker 沙箱防护边界，插件自身无法读取飞牛 NAS 上的物理 USB 串口（`/dev/ttyACM2`），数据链路尚未闭环。

---

## 2. 《硅基涌现》现有架构瓶颈分析

对照《硅基涌现》源码（`src/modules/plugin-platform/`）排查发现：

1. **Worker 线程高度沙箱化（隔离性强，无硬件 I/O）**：  
   `adapters/worker/plugin-worker-bootstrap.mjs` 中对第三方 Worker 注入了严格的 Module Loader Hook：
   * 严禁加载 `node:fs`、`node:net`、`node:child_process` 等底层模块；
   * `globalThis.fetch` 与 `globalThis.WebSocket` 被强制重置为 `undefined`；
   * 插件仅暴露 `@silicon/plugin-sdk` 的 `storage`（只读写 PostgreSQL 的 `plugin_namespaces` 表）。
2. **硬件通信能力（Hardware Capability）尚属空白**：  
   当前 `CapabilityCatalogV1` 与权限白名单（`ALLOWED_PERMISSIONS`）仅规划了存储、资产等权限，尚未对物理串口（SerialPort / USB CDC-ACM）定义受控的 Capability。
3. **外部向插件命名空间注入数据缺乏标准开放端点**：  
   目前向插件的 `plugin.storage.cache` 注入数据依赖系统内部的 `executePluginOperation`，外部宿主伴生进程无法直接调用标准 HTTP API 将硬件最新快照推送给插件。

---

## 3. 核心诉求与推荐落地方案（二选一建议）

为了让《硅基涌现》具备连接物理硬件（如 4G 网关、环境传感器、红外设备等）的能力，建议平台团队在以下两种路线中评估选择：

### 方案 A（推荐）：后端提供原生受控硬件代理通道（Native Hardware Capability）

* **实现逻辑**：
  1. 飞牛 EVO4 Docker 启动时，通过容器参数直通串口设备（`--device /dev/ttyACM2:/dev/ttyACM2`）；
  2. 在《硅基涌现》宿主后端（`server/composition/` 或 `server.mjs`）引入轻量串口驱动（如 `serialport`）常驻监听串口；
  3. **交互方式**：
     * **方式 1（指令代理）**：当前端工作区触发 `refresh` 或 `fetch_otp` 命令时，由后端在转发给 Worker 的 `request` 中附带串口采集的实时状态；
     * **方式 2（扩展 Capability）**：向插件 SDK 开放受控的硬件通道能力（如 `gateway.cellular`），经授权后允许 Worker 通过特定 SDK API 请求硬件状态。
* **收益**：
  * 架构高度内聚，用户在飞牛 EVO4 上**只需运行单个《硅基涌现》Docker 容器**，开箱即用，体验极致丝滑。

---

### 方案 B：开放受控的内部存储注入 API（Internal Storage Injector API）

* **实现逻辑**：
  1. 保持 Worker 纯净沙箱规范与 Module Loader Hook 不变；
  2. 在后端路由（`server/routes/plugin-platform.mjs`）增加一个受保护的内部更新接口：
     * `POST /api/plugin-platform/plugins/:pluginId/namespaces/:kind`
     * *(限制仅允许内网或特定 API Token 访问)*；
  3. 宿主系统运行一个极轻量的伴生进程（如 15MB 内存的 Python 网关中枢），负责采集串口数据并通过此接口将最新快照 `upsert` 到该插件的 `cache` 命名空间中；
  4. 插件 Worker 在前端点击 `[刷新状态]` 时，直接通过现成的 `await storage.cache.get("gateway_snapshot")` 即可拿到最新数据并完成 Surface 渲染。
* **收益**：
  * 《硅基涌现》核心无需集成串口底层状态机，实现代价极小（仅需增加一个路由处理函数），安全物理隔离在系统外围。

---

## 4. 硬件端通信协议规范契约（串口与数据格式）

若平台团队采纳**方案 A**，硬件通信的协议规格完全标准化，可直接依此实现：

### 4.1 物理串口配置
* **设备端口**：`/dev/ttyACM2`（Linux/fnOS 飞牛环境）或 `COM8`（Windows 环境）；
* **通信速率**：`115200 bps`, `8 数据位`, `1 停止位`, `无校验 (8-N-1)`；
* **流控与握手（关键）**：打开串口连接时**必须显式拉高 DTR 与 RTS 信号**（`dtr=true, rts=true`），否则无法接收模组的虚拟串口数据。

### 4.2 数据交互格式（行分隔 NDJSON）
所有上位机下发指令与板端应答均为以 `\n` 结尾的标准单行 JSON。

#### 1. 查询硬件看板与网络状态
* **下发指令**：
  ```json
  {"action": "get_status"}
  ```
* **硬件应答**：
  ```json
  {
    "status": "ok",
    "model": "Air780EPV",
    "csq": 28,
    "rsrp": -86,
    "temp": 39.5,
    "vbatt": 4.02,
    "rndis": false,
    "cellular_data": false,
    "storage_count": 12,
    "uptime_sec": 3600
  }
  ```

#### 2. 读取脱机黑匣子中的历史短信（LittleFS 环形队列）
* **下发指令**：
  ```json
  {"action": "get_history", "limit": 20, "keyword": null}
  ```
* **硬件应答**：
  ```json
  {
    "status": "ok",
    "total": 12,
    "returned": 1,
    "messages": [
      {
        "phone": "+8613800000000",
        "time": "2026-09-10 15:30:00",
        "content": "【招商银行】您的账户于09月10日完成消费..."
      }
    ]
  }
  ```

#### 3. 短信到达主动事件广播（基站空中推送）
当基站接收到短信时，板端会瞬时向串口广播事件：
```json
{
  "event": "sms_received",
  "phone": "10010",
  "time": "2026-09-10 15:35:10",
  "content": "【中国联通】您的动态验证码为 889900，请勿泄露。"
}
```

---

## 5. 联调测试准备与技术支持

随身网关项目侧已提供全套支撑资源，可随时配合平台团队联调：
1. **真机固件与设备**：出厂搭载纯信令 0 流量保号固件，直插免驱；
2. **模拟器与验证脚本**：支持软件模拟串口发送全量 NDJSON 数据包；
3. **最新合规插件安装包**：`dist/cellular-gateway.se-plugin`（包哈希 `818cf7430d686465...`），完全符合 v4.0 规范；
4. **测试套件**：`test_silicon_plugin.py`（提供 Manifest、沙箱边界与 Surface DTO 契约验证）。
