# ⚡ 合宙 Air780 系列智能通信网关 (Smart Cellular Gateway)

<div align="center">

[![Release](https://img.shields.io/badge/Release-v1.2.9-brightgreen.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hardware](https://img.shields.io/badge/SoC-Air780EPV%20%7C%20Air780EPM%20%7C%20Air780E%2FEC-orange.svg)](#-硬件准备与极简选型)
[![Firmware](https://img.shields.io/badge/Firmware-LuatOS--SoC%20V2050%20%7C%20V2001%20%7C%20V1124-red.svg)](#-三大核心资产一览)
[![Desktop App](https://img.shields.io/badge/Desktop-Standalone%20Exe%20%28~55MB%29-success.svg)](#quickstart)
[![AI Protocol](https://img.shields.io/badge/AI%20Protocol-FastMCP%20Ready-purple.svg)(specs/api-and-integration-guide.md)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/ocean1798/Air780EPV-Smart-Gateway/pulls)

**让合宙 4G 模组秒变随身通信网关 | 验证码 0.1秒直写剪贴板 | 0流量纯信令保号 | 单文件绿色免装上位机**

[🚀 3分钟快速上手](#quickstart) • [📦 核心资产下载](#core-deliverables) • [📱 硬件选型](#hardware) • [🗺️ 项目全景文档地图](#doc-map) • [🛑 必看避坑指南](#pitfalls)

</div>

---

<details>
<summary><b>📖 English Abstract (Click to expand)</b></summary>

> **Air780-Smart-Gateway** is an industrial-grade, zero-traffic 4G Cat.1 cellular dongle & IoT communication gateway powered by Air780EPV (EC718PV), Air780EPM (EC718PM), and Air780E/EC (EC618) running LuatOS.
> It features **native VoLTE / SMS over IMS**, **broadband-priority proxy push with Push ACK** (0 cellular traffic consumed when connected to a host PC), **multi-channel notification dispatch** (Feishu/DingTalk/WeCom/Bark/Custom Webhook), **anti-OOM cursor pagination**, **FastMCP AI Agent integration**, and an out-of-the-box **standalone ~55MB Windows desktop app**.

</details>

---

## 💡 为什么需要本项目？ (三大核心爽点)

1. ⚡ **验证码极速直写剪贴板，双手不离键盘**  
   随身短信棒插在电脑 USB 口，来信自动提炼纯净动态码并 **0.1 秒直写 Windows 剪贴板**。屏幕弹出提示后直接按 `Ctrl + V` 完成登录，彻底告别频繁翻找手机看短信。
2. 🛡️ **0 流量纯信令保号，杜绝扣费停机**  
   专为无流量卡、香港卡、大王卡副卡及保号卡设计。插电脑时模组 4G 蜂窝数据**出厂彻底掐死**，强制借用电脑宽带代推飞书/微信（Push ACK 机制），**常年插电脑使用蜂窝流量消耗严格为 0 字节**。
3. 💻 **单文件免装桌面 Exe，即插即用**  
   无需配置繁琐的 Python 环境或驱动，单个绿色 Exe 双击即用，系统托盘静默常驻，自动提供高保真 Web 管理看板与多模组集群感知。

---

<a id="core-deliverables"></a>
## 📦 三大核心资产一览 (Core Deliverables)

| 资产类型 | 版本 / 规格 | 获取位置 | 核心说明 |
| :--- | :---: | :--- | :--- |
| **1️⃣ 最新业务固件** | **`v1.2.9`** | `deploy/smart-gateway-780epv/` | **单一通用代码库**。11个 Lua 脚本开机自动识别芯片，自适应适配 VoLTE 语音与短信提取。 |
| **2️⃣ 官方底层包 (Core)** | **三大硬件标准版** | `deploy/core/` | 官方正式标准底包（详见下方选型表），均附带 64 位 SHA256 校验和。 |
| **3️⃣ 独立桌面上位机** | **单文件免装版** | `tools/host_gateway/dist/Air780EPV-Gateway.exe` | 约 55MB，内置 Python 运行时与 Web 服务，双击直开 `17801` 端口管理界面。 |

---

<a id="hardware"></a>
## 📱 硬件准备与极简选型

网关代码已实现全芯片自适应。请根据您的需求与手头硬件，在烧录时选取对应的底层包：

| 硬件型号 | 芯片架构 | 适用场景与核心能力 | 对应官方底包 (位于 `deploy/core/`) | 64位 SHA256 校验和 |
| :--- | :--- | :--- | :--- | :--- |
| **Air780EPV** | 移芯 EC718PV<br>(二代旗舰) | **【全能主力·推荐】** 全网通（含电信），唯一支持 **VoLTE 电话紧急振铃**与语音 Codec。 | `LuatOS-SoC_V2001_EC718PV_CLOUD.soc` | `1b9e8ea59e6aa5ca2de06f71be04359a5024d41565f044b2120b50d5a53f91e5` |
| **Air780EPM** | 移芯 EC718PM<br>(二代精简) | **【性价比之选】** 纯贴片模组低功耗，主打高性价比短信网关。（⚠️ 严禁选 109 固件） | **`LuatOS-SoC_V2050_Air780EPM_103.soc`** | `ce640ab243f9d1721ee1ace00e377a02ea75df3ec43dfa66b9de463626e653b2` |
| **Air780E / EC** | 移芯 EC618<br>(一代经典) | **【已有设备利用】** 一代经典板卡（如 `EVB_Air780X_V1.5`），不支持电信短信；短接 S2 可插电自启。 | `LuatOS-SoC_V1124_EC618.soc` | `3af84bccfdfba8d83f8f5a03d721f268c8bd3fc82470e0681732ba2c43b85d2a` |

---

<a id="quickstart"></a>
## 🚀 3 分钟极速开箱上手 (Quickstart)

新手拿到硬件后，只需按以下 **3 步** 即可投入使用：

```mermaid
flowchart LR
    Step1["1. 插上电脑 Type-C<br>(EC618长按POW开机)"] --> Step2["2. 打开 Luatools<br>选择预置 ini 点烧录"] --> Step3["3. 插卡运行 Exe<br>浏览器打开 17801 开始用!"]
```

### 步骤 1：连接电脑
- 使用全功能 Type-C 数据线将模组连接到电脑 USB 口；
- *注：如果是 Air780E/EC 开发板，插上后请长按板载 `S2 (POW)` 键 2 秒开机（红色电源灯亮起）。*

### 步骤 2：一键烧录
1. 下载并打开合宙官方烧录工具 **Luatools_v3**；
2. 点击右上角 **【项目管理测试】**，直接选中本项目预置的专属工程：
   - Air780EPM 选 ➔ **`tools/Luatools/project/Air780EPM智能网关.ini`**
   - Air780EPV 选 ➔ **`tools/Luatools/project/smart-gateway-780epv.ini`**
   - Air780E/EC 选 ➔ **`tools/Luatools/project/Air780EC智能网关.ini`**
3. 勾选左下角“添加底层和提示下载”，点击右下角 **【下载底层和脚本】** 即可完成灌入！  
   *(若未自动响应，按住模组板载的 BOOT 键再插拔一次 USB 即可触发)*

### 步骤 3：插卡即用
1. 模组断电插入 Nano-SIM 卡，重新插上电脑；
2. 双击运行 **`tools/host_gateway/dist/Air780EPV-Gateway.exe`**；
3. 本地浏览器自动调起 **`http://127.0.0.1:17801`**，卡槽信号就绪，即刻开始收发短信！

---

<a id="doc-map"></a>
## 🗺️ 项目全景文档地图 (Documentation Map)

为了保持首页极简，更多深度教程与技术规范已拆解至专门文档，请对号入座查阅：

| 你的身份 / 诉求 | 推荐阅读入口 | 你将获得什么？ |
| :--- | :--- | :--- |
| 🟢 **普通用户 / 刚买模块** | [🚀 3分钟极速开箱主干](#quickstart) | 零代码开箱主干，照着 1-2-3 步直接点亮网关 |
| 📱 **配置推送渠道** | [📖 多渠道推送配置指南](specs/push-channels.md) | 飞书交互卡片、钉钉、企业微信、Bark 及自定义通用 Webhook 完整配置与 JSON 样例 |
| 🛠️ **排错 / 端口未识别** | [🛠️ 故障排查与自愈手册](specs/troubleshooting.md) | USB 识别、驱动安装、网络注册失败、端口冲突全链路排错清单 |
| ⚡ **硬件改线 / 强刷救砖** | [⚡ 烧录工具链与救砖工序](specs/flashing-and-toolchain.md) | S1(BOOT) 光刻 BootROM 强刷救砖、S2(POW) 接地改线实现插电自启动 |
| 🔌 **硬件引脚 / 串口拓扑** | [🔌 硬件通信基线与规格书](specs/hardware-spec.md) | 模组引脚定义、真实 USB 虚拟串口映射 (COM6/7/8) 与各芯片实测功耗 |
| 🤖 **AI Agent / 二次开发** | [🔌 REST API 与 FastMCP 规格](specs/api-and-integration-guide.md) | 17801 端口 HTTP API 接口、Claude Desktop / Cursor MCP 智能体接入说明 |
| 🧠 **嵌入式 / 架构极客** | [🏗️ 微内核网关架构设计](specs/smart-gateway-architecture.md) | 128KB RAM 游标防 OOM 机制、LittleFS 脱机黑匣子存储与双保底时序设计 |

---

<a id="pitfalls"></a>
## 🛑 开发者必看避坑指南 (Top 3)

1. **Air780EPM 严禁烧录 `109` 固件，必须使用 `103` 标准底包**  
   合宙原厂的 `_109.soc` 为随身 WiFi 网卡定制版，**移除了 `sms` 短信底层库**。插卡入网瞬间调用短信服务会触发空指针崩溃，引发看门狗循环复位死锁。请务必选 `_103.soc` 标准底包。
2. **严禁芯片跨代混刷**  
   一代 EC618（Air780E/EC）只能刷 `V11xx` 固件；二代 EC718（Air780EPM/EPV）只能刷 `V20xx` 固件，两代指令集与引导地址不兼容，跨代混刷将黑屏无法开机。
3. **一代 EC618 开发板开机机制**  
   `EVB_Air780X_V1.5` 开发板出厂默认插电关机，插上 USB 后必须**长按 S2 (POW) 键 2 秒开机**；如需做成随身网关插电自启，将板上 S2 按键短接接地即可。

---

## 📄 开源协议 (License)

本项目遵循 [MIT 开源协议](LICENSE)。欢迎提交 Issue 与 Pull Request 共同完善！
