# Air780EPV 蜂窝通信网关 - 工业级 Model Context Protocol (MCP) 服务

基于 Anthropic 官方 FastMCP 规范构建，采用 **透明自托管 Hub-and-Spoke** 架构，彻底消除 Windows 物理串口独占死锁，赋予外部 AI Agent（Claude Desktop / Cursor / 工作区 Agent 等）直接操作真实物理蜂窝模组的能力。

---

## 1. 架构特性：透明自拉起 Hub 共享中枢

* **多端并发零冲突**：物理串口 `COM8` 由后台 `gateway_hub.py` 单例独占管理，通过本地回环 IPC (`127.0.0.1:17800`) 广播。桌面控制台 `gateway_client.py` 与 Claude Desktop 可**同时在线、共享通信**。
* **零运维心智负担 (Auto-spawn)**：MCP 服务启动时若检测到中枢未就绪，会自动静默在后台拉起 Hub，用户无需手动开启或维护额外的黑窗口。
* **消除空中时延假阴性**：`cellular_wait_for_otp` 具备 3 分钟新鲜度与事件挂起守候机制，短信到站瞬间毫秒级精准唤醒，告别“刚发验证码 AI 就回复未收到”的断层体验。
* **Stdio 纯净度保证**：内部调试日志全量重定向至 `sys.stderr`，绝不污染 JSON-RPC 2.0 报文管道。

---

## 2. 导出的 MCP 原语清单

### A. Tools 工具列表（大模型自主调用）

| 工具名称 | 核心参数与默认值 | 作用与时序说明 |
| :--- | :--- | :--- |
| **`cellular_wait_for_otp`** | `timeout_seconds: 20`<br>`freshness_seconds: 180` | **智能守候验证码**：检查过去 3 分钟内未消费验证码；无则挂起守候（最大等待 20s），短信到站瞬间毫秒级唤醒提取。 |
| **`cellular_get_status`** | 无 | 获取网关运行看板（蜂窝信号 CSQ/RSRP、核心温度、供电电压、黑匣子存量、随身上网状态、连续开机时间）。 |
| **`cellular_send_sms`** | `phone` (号码)<br>`content` (正文) | 驱动 4G 射频主动向目标手机号发送短信，监听基站排队回执。 |
| **`cellular_get_history`** | `limit: 20`<br>`keyword: None` | 查阅板载 128KB LittleFS 脱机黑匣子短信存档（断电不丢，支持关键词搜索）。 |
| **`cellular_clear_sms_history`**| 无 | 清空板载 LittleFS 脱机黑匣子短信存档。 |
| **`cellular_toggle_rndis`** | `enable: bool` | 受控开启/关闭 4G 随身上网（带 USB 协议栈热复位避让）。 |
| **`cellular_reboot_gateway`** | `reason: str` | 向网关下发软复位指令，安全重启模组。 |

### B. Resources 资源列表（上下文只读数据源）

* **`cellular://gateway/status`**：实时设备运行全景看板 JSON。
* **`cellular://sms/latest`**：最新一条接收到的短信详情纯文本。
* **`cellular://sms/history`**：板载脱机黑匣子中的短信流 JSON。

### C. Prompts 提示词模版

* **`carrier_query`**：预置运营商业务查询交互助手（10010/10086/10001 查话费、查流量）。
* **`otp_verification`**：标准动态验证码监听提取助手。

---

## 3. 外部 AI 客户端一键接入指南

### A. Claude Desktop 接入
打开配置文件（Windows: `%APPDATA%\Claude\claude_desktop_config.json`）：
```json
{
  "mcpServers": {
    "air780epv-cellular": {
      "command": "python",
      "args": [
        "tools/mcp_server/server.py"
      ]
    }
  }
}
```
保存后完全重启 Claude Desktop 即可。

### B. Cursor 接入
在 Cursor 的 **Settings > Features > MCP Servers** 中点击 **Add New MCP Server**：
* **Name**: `air780epv-cellular`
* **Type**: `command`
* **Command**: `python tools/mcp_server/server.py`

---

## 4. 自然语言调用场景

配置完成后，你可以随时对 AI 说：
* *“刚发了验证码，帮我守候一下并告诉我结果”* -> AI 自动调用 `cellular_wait_for_otp` 并在短信到站后秒级返回；
* *“给 10010 发短信查一下话费余额”* -> AI 自动调用 `cellular_send_sms`；
* *“看看通信网关现在的信号强度和温度”* -> AI 自动调用 `cellular_get_status`；
* *“把网关的 4G 随身上网打开”* -> AI 自动调用 `cellular_toggle_rndis(enable=True)`。
