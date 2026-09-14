# Air780EPV 智能通信网关 - 插件接入与 API 开发者参考手册

本手册为 Air780EPV 蜂窝通信网关的完整接入规范。系统提供两种接入模式：
* **模式 A（AI Agent MCP 协议）**：面向 Claude Desktop、Cursor、Cline、AI 工作流等大模型宿主，通过标准 JSON-RPC 2.0 stdio 管道暴露物理工具；
* **模式 B（本地 IPC TCP 接口）**：面向 Python 自定义脚本、NAS Docker 守护容器、物联网桥接等，通过 `127.0.0.1:17800` 进行行分隔 NDJSON 双向通信。

---

## 一、 模式 A：AI Agent MCP 插件接入指南

### 1. 客户端配置文件接入

#### (1) Claude Desktop
打开配置文件：
* **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
* **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

在 `mcpServers` 节点中添加：
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
*保存后完全退出并重启 Claude Desktop，聊天窗口右下角出现工具锤子图标即表示加载成功。*

#### (2) Cursor IDE
在 Cursor 中打开 **Settings > Features > MCP Servers**，点击 **Add New MCP Server**：
* **Name**: `air780epv-cellular`
* **Type**: `command`
* **Command**: `python tools/mcp_server/server.py`

---

### 2. MCP Tools（大模型主动调用工具库）

所有工具均具备标准中文 Docstring 和严格类型提示。若操作失败，将在 MCP 协议层自动抛出带 `isError: true` 的异常信息供模型自愈。

---

#### 2.1 `cellular_wait_for_otp`：智能时序守候与提取验证码
* **作用**：解决运营商 4G 空中 3~8 秒传输时延的核心工具。优先提取近时验证码，若无则挂起守候，短信到站瞬间毫秒级自动唤醒。
* **参数**：
  | 参数名 | 类型 | 必填 | 默认值 | 说明 |
  | :--- | :--- | :---: | :---: | :--- |
  | `timeout_seconds` | `int` | 否 | `20` | 最大守候时间（秒）。超过该时间未收到将抛出超时异常。 |
  | `freshness_seconds`| `int` | 否 | `180`| 有效验证码的新鲜度窗口（秒，默认 3 分钟）。 |
* **成功返回示例**（JSON 字符串）：
  ```json
  {
    "status": "SUCCESS",
    "otp_code": "773322",
    "sender": "+8613800138000",
    "received_at": "2026-09-10 12:17:38",
    "age_seconds_ago": 2,
    "raw_content": "【工业级MCP验证】您的安全动态验证码为：773322，3分钟内有效。"
  }
  ```
* **自然语言调用触发词**：*“帮我守候一下验证码”、“看一下刚收到的验证码是多少”*。

---

#### 2.2 `cellular_get_status`：获取硬件全景看板
* **作用**：主动查阅网关硬件运行指标、蜂窝信号质量与随身上网状态。
* **参数**：无
* **成功返回示例**（JSON 字符串）：
  ```json
  {
    "hardware": {
      "model": "Air780EPV",
      "temperature_celsius": "37.00",
      "voltage_vbat": "4.00 V",
      "lua_memory_used_kb": "64 KB / 128 KB"
    },
    "cellular_network": {
      "network_ready": true,
      "csq_signal": 27,
      "rsrp_dbm": "-89 dBm"
    },
    "system_status": {
      "rndis_internet_sharing": "🟢 已开启 (USB 虚拟网卡在线)",
      "blackbox_sms_count": 3,
      "continuous_uptime": "0时 21分 40秒",
      "daily_reboot_policy": "未开启"
    }
  }
  ```

---

#### 2.3 `cellular_send_sms`：主动发射 4G 短信
* **作用**：驱动 4G 射频发射短信，并阻塞监听基站发送队列 ACK。
* **参数**：
  | 参数名 | 类型 | 必填 | 说明 |
  | :--- | :--- | :---: | :--- |
  | `phone` | `str` | 是 | 接收方手机号码（如 `+8613800000000` 或 `10010`） |
  | `content` | `str` | 是 | 短信文本正文内容 |
* **成功返回示例**：
  ```text
  ✅ 短信已成功提交至 4G 基站发送队列！目标: 10010，正文: '101'
  ```

---

#### 2.4 `cellular_get_history`：查阅板载脱机黑匣子短信
* **作用**：读取模组内部 128KB LittleFS 安全存储的短信存档（断电不丢失）。
* **参数**：
  | 参数名 | 类型 | 必填 | 默认值 | 说明 |
  | :--- | :--- | :---: | :---: | :--- |
  | `limit` | `int` | 否 | `20` | 最大拉取条数 |
  | `keyword` | `str` | 否 | `None` | 按手机号或正文模糊检索关键词 |
* **成功返回示例**（JSON 字符串）：
  ```json
  {
    "total_archived": 12,
    "returned_count": 1,
    "filter_keyword": "验证码",
    "messages": [
      {
        "sender": "+8613800138000",
        "time": "2026-09-10 12:17:38",
        "otp": "773322",
        "content": "【工业级MCP验证】您的安全动态验证码为：773322，3分钟内有效。"
      }
    ]
  }
  ```

---

#### 2.5 `cellular_toggle_rndis`：受控启闭 4G 随身上网
* **作用**：动态开启或关闭 USB 虚拟网卡（RNDIS），出厂默认严格关闭以防偷跑流量。
* **参数**：
  | 参数名 | 类型 | 必填 | 说明 |
  | :--- | :--- | :---: | :--- |
  | `enable` | `bool` | 是 | `true` 为开启 4G 上网；`false` 为关闭 4G 上网 (0流量偷跑) |
* **成功返回示例**：
  ```text
  ✅ 指令已执行: 正在开启 4G 随身上网... 模组已启动 USB 协议栈热复位，预计 5 秒内完成重连。
  ```

---

#### 2.6 `cellular_reboot_gateway`：安全重启硬件模组
* **参数**：`reason: str`（默认 `"mcp_agent_action"`）
* **成功返回示例**：`✅ 已成功向网关下发重启指令 (原因: mcp_agent_action)，模组将在 1 秒内安全复位并重新驻网。`

#### 2.7 `cellular_clear_sms_history`：清空脱机黑匣子
* **参数**：无
* **成功返回示例**：`✅ 板载 LittleFS 脱机黑匣子短信存档已彻底清空！`

---

#### 2.8 `cellular_toggle_board_data`：受控启闭板载 4G 蜂窝数据
* **作用**：切换模组自身 4G 蜂窝数据通信（出厂默认彻底掐断，0 流量保号防线，由电脑宽带代推）。
* **参数**：
  | 参数名 | 类型 | 必填 | 说明 |
  | :--- | :--- | :---: | :--- |
  | `enable` | `bool` | 是 | `true` 为开启板端 4G 数据；`false` 为掐断数据 (0流量纯信令保号) |
* **成功返回示例**：
  ```text
  ✅ 指令已执行: 已掐断板载蜂窝数据 (0流量纯信令保号模式，由电脑宽带代推)。
  ```

---

### 3. MCP Resources（上下文只读资源）

大模型客户端支持通过 `@` 直接把资源上下文附在 Prompt 中，无需发起 Tool Call：
* **`cellular://gateway/status`** (MIME: `application/json`)：网关实时硬件状态。
* **`cellular://sms/latest`** (MIME: `text/plain`)：最新一条接收到的短信详情。
* **`cellular://sms/history`** (MIME: `application/json`)：脱机黑匣子短信全量归档。

---

### 4. MCP Prompts（预置交互模版）

* **`carrier_query`**：预置三大运营商业务查询指令模版（10010/10086/10001 查话费、查流量）。
* **`otp_verification`**：动态验证码守候与标准格式化输出模版。

---

## 二、 模式 B：本地 IPC TCP 接口接入指南 (127.0.0.1:17800)

面向不需要经过大模型直接进行代码调用的场景（如本地自动化脚本、NAS 守护进程、自建 Web 控制台）。

### 1. 传输与协议规范
* **协议与端点**：TCP Stream `127.0.0.1:17800`（仅监听本机回环，无网络暴露风险）
* **报文编码**：UTF-8，行分隔 NDJSON（以 `\n` 结尾）
* **自愈特性**：客户端连接失败时，静默调用 `tools/host_gateway/gateway_hub.py` 即可自动后台拉起。

### 2. 请求与响应报文格式 (Request-Response)
客户端主动发送：
```json
{"type": "cmd", "id": "req_001", "cmd": "send_sms", "params": {"phone": "10010", "content": "101"}}
```
中枢即时回执：
```json
{"type": "res", "id": "req_001", "code": 0, "msg": "QUEUED_TO_BASE_STATION", "data": {}}
```

#### 支持的指令集一览：
| `cmd` 命令名 | `params` 关键参数 | 作用说明 |
| :--- | :--- | :--- |
| `get_status` | `{}` | 查询网关温度、电压、CSQ/RSRP、RNDIS状态、Uptime全景 |
| `send_sms` | `{"phone": "号码", "content": "正文"}` | 驱动 4G 射频发送短信 |
| `get_history` | `{"limit": 20}` | 查阅脱机黑匣子存储的最新短信列表 |
| `clear_history` | `{}` | 清空板载黑匣子 |
| `set_rndis` | `{"enable": true/false}` | 启闭 4G 随身上网（模式 3 虚拟网卡） |
| `set_cellular_data` | `{"enable": true/false}` | 启闭板载 4G 蜂窝数据通信（默认 false，0流量纯信令保号） |
| `reboot` | `{"reason": "原因"}` | 立即软重启硬件 |
| `get_uptime` | `{}` | 获取精确单调运行秒数与下次自动重启倒计时 |
| `set_reboot_policy` | `{"daily_hour": 3}` | 设置每天固定重启小时（-1 为关闭，0~23 为对应整点） |

### 3. 广播事件报文格式 (Pub-Sub Push)
当板端产生事件时，中枢会**向所有连接在 17800 的客户端实时广播**：
* **收到短信事件**：
  ```json
  {"type": "event", "event": "sms_rx", "data": {"from": "+8613800138000", "content": "您的验证码是 123456", "code": "123456", "time": 1789013000}}
  ```
* **来电拦截事件**：
  ```json
  {"type": "event", "event": "call_rx", "data": {"from": "10010", "action": "hang_up_success"}}
  ```
* **开机就绪事件**：
  ```json
  {"type": "event", "event": "gateway_ready", "data": {"bsp": "Air780EPV", "number": "+8613800138000", "csq": 27, "temp": "37.00"}}
  ```

---

### 4. 极简 Python 直连调用示例 (10行代码)

```python
import socket, json

# 1. 建立 IPC 连接
s = socket.socket()
s.connect(("127.0.0.1", 17800))

# 2. 下发发短信指令
req = {"type": "cmd", "id": "demo_1", "cmd": "send_sms", "params": {"phone": "10010", "content": "101"}}
s.sendall(json.dumps(req).encode("utf-8") + b"\n")

# 3. 接收结果
response = json.loads(s.recv(4096).decode("utf-8"))
print("网关响应:", response)
s.close()
```

---

## 三、 Web 管理控制台与 RESTful / SSE 接口规范 (端口 17801)

为满足局域网免客户端、手机/平板跨端浏览器监控与调试需求，网关宿主端提供基于标准库实现的轻量级 Web 控制台服务 (`gateway_web.py`)。

### 1. 服务架构与设计哲学
* **彻底杜绝板端内存溢出 (OOM) 风险**：严禁在板端 128KB 极小内存下运行 HTTP/Web 服务器，所有静态资产、RESTful 路由、多并发客户端管理均由宿主机（PC/NAS）承担；
* **零外部依赖自包含**：纯 Python 标准库 (`http.server.ThreadingHTTPServer` + `socket` + `json`)，纯单文件原生现代响应式 HTML5/CSS3/ES6 前端，零 node_modules / npm 构建依赖；
* **全双工双通道**：
  * **出向控制 (Control Plane)**：通过 RESTful API（GET / POST）调用；
  * **入向推流 (Telemetry Plane)**：通过 Server-Sent Events (`/api/events`) 维持毫秒级轻量推流通道，收到短信或拦截来电时瞬间广播至浏览器。

### 2. HTTP RESTful 路由清单

| 请求方法 | 接口路径 | 说明 | 请求体 / 查询参数 | 返回数据摘要 |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/` | 控制台单页应用 (SPA) | 无 | HTML5 静态资产 |
| `GET` | `/api/status` | 获取网关全景遥测看板 | 无 | `{ok: true, data: {csq, temp, vbat, rndis_enable, cellular_data_enable, uptime, ...}}` |
| `GET` | `/api/history` | 查询脱机黑匣子短信（支持游标分页与防 OOM 防御） | `limit=15&cursor=h:gen:offset` (非法游标自动清洗防御) | `{ok: true, data: {list: [...], cursor: "...", has_more: bool, total: N}}` |
| `GET` | `/api/events` | SSE 实时事件订阅管道 | 无 | `text/event-stream` 长连接，心跳 15s |
| `POST` | `/api/sms/send` | 驱动 4G 射频主动代发短信 | `{"phone": "...", "content": "..."}` | `{ok: true, data: {...}}` |
| `POST` | `/api/control/rndis` | 动态启闭 4G 随身上网 | `{"enable": true/false}` | `{ok: true, data: {rndis: bool}}` |
| `POST` | `/api/control/data` | 切换板端蜂窝数据(0流量保号)| `{"enable": true/false}` | `{ok: true, data: {cellular_data: bool}}` |
| `POST` | `/api/control/reboot` | 安全向模组下发软复位 | `{"reason": "..."}` | `{ok: true, data: {...}}` |
| `POST` | `/api/control/clear_history`| 清空板载黑匣子短信 | `{}` | `{ok: true, data: {total: 0}}` |

### 3. SSE 实时事件数据流定义
客户端连接 `/api/events` 后，中枢在捕获硬件串口信令时主动分发事件块：
* **`sms_received`**：`event: sms_received\ndata: {"phone":"...","content":"...","otp":"...","time":"..."}\n\n`
* **`call_incoming`**：`event: call_incoming\ndata: {"phone":"...","time":"...","action":"rejected"}\n\n`
* **`status_update`**：`event: status_update\ndata: {"csq":25,"temp":"32.00",...}\n\n`

### 4. 启动方式
* **Windows 桌面**：双击运行 `tools/host_gateway/run_web.bat`；
* **命令行**：`python tools/host_gateway/gateway_web.py --port 17801`；
* **访问地址**：
  * 本地浏览器：`http://127.0.0.1:17801`
  * 局域网其他设备：`http://<宿主机IP>:17801`

---

## 四、 避坑指南与核心设计原则

1. **串口独占与动态自愈原则**：
   * 严禁任何第三方程序直接打开板端用户虚拟串口（由 Hub 动态自适应探测并独占接管，例如 `COM8`/`COM16`，VUART_0 用户口）！该端口必须且仅由 `gateway_hub.py` 单例管理；
   * 所有上位端（CLI、MCP、脚本、Web）均应通过 `127.0.0.1:17800` 本地 IPC 连接，实现多端无锁安全并发。
2. **集群多卡槽寻址与防串台约束**：
   * 在多设备集群版（`cluster_gateway`）中，所有 REST 接口与 IPC 请求均支持传入 `slot` 参数（如 `/api/status?slot=2`）；
   * 若指定卡槽离线，系统严格返回 `None` / 设备离线报错，坚决不静默降级回退到 `slot_1`，杜绝跨卡槽数据串台。
3. **随身上网（RNDIS）防死锁与时序避让**：
   * 开启或关闭 RNDIS 时，模组会经历约 5 秒的 USB 虚拟总线热复位断开与重新枚举；
   * Hub 会自动执行防抖避让，在此期间请勿连续频繁敲击切换指令。
3. **验证码提码守候最佳实践**：
   * 严禁在发短信后立即使用瞬时查询；
   * 强烈推荐使用 `cellular_wait_for_otp`，设定 20~25 秒超时与 180 秒新鲜度，享受毫秒到站即刻唤醒的最佳体验。
