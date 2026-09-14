# 架构方案：Air780EPV 通信网关工业级 MCP 升级与共享中枢演进

## 1. 方案背景与解决的问题

在真机完成 MCP 基础链路验证后，针对工业级落地与日常使用痛点进行审查，识别出以下核心问题：
1. **物理串口互斥死锁**：Windows 下 `COM8` 独占，导致桌面控制台与 Claude Desktop 无法同时运行。
2. **MCP Resources 无法被大模型自主调起**：若纯粹按文档将状态抽离为 Resource，主流客户端（Claude/Cursor）中的大模型无法自主发起查询。
3. **空中时延导致的验证码误判**：4G 基站投递存在 3~8 秒空中耗时，瞬时读取容易导致 AI 提前做出“未收到验证码”的假阴性判定。
4. **独立进程的运维心智负担**：若需要用户单独命令行常驻守护进程，容易造成进程遗忘、残留与启动摩擦。

---

## 2. 核心架构设计：透明自托管 Hub-and-Spoke 体系

借鉴 Android 调试桥（ADB Daemon）成熟架构，采用**“客户端透明自拉起 + 本地 IPC 共享”**模式，杜绝手动运维与串口竞态：

```text
                 ┌──────────────────────────────┐
                 │ 物理模组 Air780EPV (USB CDC)  │
                 └──────────────┬───────────────┘
                                │ COM8 物理串口 (独占 NDJSON / 115200)
                                ▼
        ┌────────────────────────────────────────────────┐
        │        网关自托管中枢 (gateway_hub.py)         │
        │  - 独占管理 COM8，负责热插拔断线自愈与避让时序  │
        │  - 内存事件分发中心 (SMS/OTP/状态多端广播)     │
        │  - 0 流量保号代理：电脑 WiFi/宽带代发飞书卡片   │
        │  - 极轻量 Localhost Socket 监听 (127.0.0.1:17800)│
        │  - 自动心跳检测，客户端透明自拉起 (Auto-spawn) │
        └───────┬────────────────┬───────────────┬───────┘
                │                │               │
       (本地 IPC Socket) (本地 IPC Socket) (本地 IPC Socket)
                │                │               │
                ▼                ▼               ▼
        ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
        │ Claude MCP   │ │ 桌面控制台   │ │ 未来 NAS/    │
        │ (FastMCP)    │ │ (CLI 客户端) │ │ Docker 守护  │
        └──────────────┘ └──────────────┘ └──────────────┘
```

### 2.1 网关中枢 (gateway_hub.py) 的五大核心职责

1. **破解 Windows 物理串口独占死锁**：
   * Windows 下 `COM8` 仅允许单进程独占打开。`gateway_hub.py` 作为唯一硬件持有者常驻占有 `COM8`，将其抽象为本地全双工 TCP Socket（`127.0.0.1:17800`），允许多个客户端（FastMCP、桌面控制台、自动化脚本）并发无锁访问。
2. **0 流量纯信令保号的“宽带代发人”**：
   * 监听板端物理信令。当板端处于 `cellular_data = false` 模式时，收到短信、来电或开机事件，由 Hub 进程利用宿主电脑现成的家庭 WiFi / 有线宽带发起飞书 Webhook 推送，实现手机卡严格 0 字节、0 扣费，且即时通知一个不少。
3. **多路事件全双工实时广播 (Pub-Sub)**：
   * 板端上报的 `sms_rx`、`call_rx`、`status` 及 `gateway_ready` 事件，Hub 瞬间广播给所有连接在 17800 端口的活跃客户端，保证控制台终端与 AI 上下文毫秒级同步。
4. **硬件插拔与重启的“防震缓冲层”（断线自愈）**：
   * 模组重启或 USB 虚拟网卡热复位时，`COM8` 物理端口会消失 2~3 秒。Hub 内部实现防抖捕获与安全退避重试，在端口重现时自动重连，屏蔽底层硬件震荡，保障上层客户端会话永不崩溃。
5. **智能提码兜底正则引擎与内存事件池**：
   * 内建覆盖“口令”、“PIN”、“OTP”、倒装句式的 Python 二次提码引擎；内存维护最新 50 条短信队列与最新 OTP 缓存，支撑 `wait_for_otp` 纳秒级极速命中。

### 2.2 透明生命周期控制（Auto-spawn）
* 上层所有应用（MCP Server、桌面控制台）**永远只连接本地 IPC 端口 `17800`**，禁止直连物理串口；
* 客户端启动时探测 `127.0.0.1:17800`：
  - 若 Hub 已在运行：毫秒级直连复用；
  - 若 Hub 未在运行：通过 `subprocess.Popen` 以非阻塞后台方式**静默自动拉起 Hub 进程**，等待 1.5 秒完成就绪自愈；
* 用户无须关心后台是否有黑窗口，任意端打开即用，多端同时在线永不崩溃。

---

## 3. MCP 原语重构与双重暴露规范

### 3.1 Tools 工具矩阵（大模型主动调用）
统一使用 `cellular_` 前缀，区分全局命名空间：

| 工具名称 | 核心参数与默认值 | 行为与异常处理 |
| :--- | :--- | :--- |
| **`cellular_wait_for_otp`** | `timeout_seconds: int = 20`<br>`freshness_seconds: int = 180` | **智能时间窗口守候**：<br>1. 优先检索过去 3 分钟内收到且未消费的验证码，若有即刻秒级返回；<br>2. 若无，挂起监听事件队列，直到收到新短信实时唤醒并提取；<br>3. 达到超时时间抛出友好提示，支持 AI 自主重试。 |
| **`cellular_get_status`** | 无 | **主动查询运行看板**：返回包含蜂窝信号 (CSQ/RSRP)、芯片温度、供电电压、脱机黑匣子存量、随身上网状态、持续运行时间的格式化 JSON。 |
| **`cellular_get_history`** | `limit: int = 20`<br>`keyword: Optional[str] = None` | **主动查询短信历史**：读取板载 LittleFS 脱机黑匣子，支持手机号/正文关键词搜索。 |
| **`cellular_send_sms`** | `phone: str`<br>`content: str` | 驱动 4G 射频发射短信，并阻塞监听基站队列接收回执。 |
| **`cellular_toggle_rndis`** | `enable: bool` | 切换 4G 随身上网虚拟网卡（带 5 秒硬件热复位避让时钟，默认关闭）。 |
| **`cellular_toggle_board_data`** | `enable: bool` | 切换模组自身 4G 蜂窝数据通信（出厂默认彻底掐断，0 流量保号防线，由电脑宽带代推）。 |
| **`cellular_reboot_gateway`** | `reason: str = "agent_action"` | 向模组下发软复位指令。 |
| **`cellular_clear_sms_history`** | 无 | 清空板载黑匣子。 |

### 3.2 Resources 资源矩阵（宿主环境只读直读）
挂载同源只读数据，供人类在客户端通过 `@` 引用，或供自动化流水线直接注入上下文：
* **`cellular://gateway/status`**：MIME `application/json`，实时设备运行状态。
* **`cellular://sms/latest`**：MIME `text/plain`，最新一条收到的短信详情。
* **`cellular://sms/history`**：MIME `application/json`，脱机黑匣子全量历史列表。

### 3.3 Prompts 交互工作流模版
* **`carrier_query`**：预置运营商查询助手，指导大模型组装 10010（联通）/ 10086（移动）/ 10001（电信）的标准查话费与流量指令。
* **`otp_verification`**：验证码监听助手，引导大模型按标准流程调用 `cellular_wait_for_otp`，并格式化输出验证码。

### 3.4 标准错误处理规范
放弃字符串伪成功反馈（如返回 `"❌ 失败"`）。底层报错时由 FastMCP 工具函数显式抛出 `ValueError` 或 `RuntimeError`，FastMCP 自动在协议层打上 **`isError: true`** 标签，使 Claude 等大模型能够明确捕捉错误并执行纠错。

---

## 4. 本地 IPC 协议设计 (Local IPC via Socket)

* **传输层**：`127.0.0.1:17800`（TCP Stream，行分隔 NDJSON）
* **协议格式**：
  - 请求 (Req)：`{"id": "req_xxx", "action": "cmd", "cmd": "send_sms", "params": {...}}\n`
  - 响应 (Res)：`{"id": "req_xxx", "code": 0, "msg": "OK", "data": {...}}\n`
  - 事件广播 (Pub)：`{"event": "sms_rx", "data": {...}}\n`（当 Hub 收到板端推送时，多路广播给当前所有在线连接的客户端）

---

## 5. 代码固化评估与实施边界

依据工作区原则，对本次涉及的代码扩展进行评估：
* **解决的问题**：彻底消除 Windows 串口互斥死锁，允许桌面控制台与 AI MCP 服务全天候并行；消除 4G 空中时延导致的验证码漏提。
* **必要性**：直接决定该蜂窝网关能否真正成为 Ocean 随时可用、与多端开发环境无缝集成的日用基础设施。
* **实施范围**：
  1. `tools/host_gateway/gateway_hub.py`：常驻中枢与串口多路复用调度器；
  2. `tools/mcp_server/gateway_driver.py`：改造为 IPC 客户端（带 Hub 自动拉起探测）；
  3. `tools/mcp_server/server.py`：升级为工业级 FastMCP 规范（Tools + Resources + Prompts）；
  4. `tools/host_gateway/gateway_client.py`：接入 IPC 客户端，验证与 MCP 同时运行。
* **维护成本**：纯 Python 标准库（`socket`, `threading`, `subprocess`），不增加第三方重型中间件依赖；端口仅监听本机回环地址（127.0.0.1），无网络暴露安全面。
