# ⚡ 合宙 Air780 4G 随身短信网关

<div align="center">

[![Release](https://img.shields.io/badge/Release-v1.2.9-brightgreen.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hardware](https://img.shields.io/badge/硬件-Air780EPV%20%7C%20Air780EPM%20%7C%20Air780E-orange.svg)](#-支持哪些硬件--买哪款)
[![Desktop App](https://img.shields.io/badge/软件-Windows%20单文件免安装-success.svg)](#-3-步快速上手)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/ocean1798/Air780EPV-Smart-Gateway/pulls)

**插上电脑自动收短信 • 验证码自动复制直接 Ctrl+V • 不走卡流量 • Windows 免安装双击即用**

[🚀 3步快速上手](#-3-步快速上手) • [📥 文件下载](#-文件下载) • [📱 硬件选型](#-支持哪些硬件--买哪款) • [🗺️ 进阶文档地图](#-进阶文档导航) • [⚠️ 避坑提醒](#-新手必看避坑)

</div>

---

<details>
<summary><b>📖 English Abstract (Click to expand)</b></summary>

> **Air780-Smart-Gateway** turns cheap 4G Cat.1 dongles (Air780EPV / Air780EPM / Air780E) into a handy desktop SMS forwarder. It auto-copies verification codes to your clipboard, pushes messages to Feishu/WeCom/DingTalk/Bark, consumes 0 cellular data when connected to PC, and runs via a single standalone Windows executable.

</details>

---

## 💡 它是干嘛的？ (解决什么痛点)

1. 📋 **验证码自动复制，不用翻手机**  
   4G 模块插在电脑 USB 口，验证码短信一来，**自动提取并存入电脑剪贴板**。屏幕右下角弹窗的同时，你双手不用离开键盘，直接按 `Ctrl + V` 就能粘贴登录。
2. 💰 **插电脑不跑卡流量，保号卡放心用**  
   专为无流量卡、香港卡（如 ClubSim）或几块钱的副卡保号设计。插在电脑上时，**自动走电脑网络转发消息，不耗费手机卡流量**，不用担心偷跑流量欠费停机。
3. 🖥️ **Windows 免安装，解压双击就能跑**  
   不用在电脑上配 Python 环境，下载单个 Exe 双击打开就能用。自带网页后台，插上多块模块也能自动识别。

---

## 📥 文件下载

| 需要什么文件？ | 在哪里拿？ | 怎么用？ |
| :--- | :--- | :--- |
| **电脑端软件 (Exe)** | `tools/host_gateway/dist/Air780EPV-Gateway.exe` | 绿色免安装，下载后双击运行即可，自动打开网页后台。 |
| **网关功能代码** | `deploy/smart-gateway-780epv/` | 最新版源码（里面有 11 个 lua 脚本，通用适配所有型号）。 |
| **模块底层包 (Core)** | `deploy/core/` | 官方底层固件，按手里的模块型号挑一个刷入（见下方选型表）。 |

---

## 📱 支持哪些硬件 / 买哪款？

手头有哪个用哪个，买新模块看下表推荐：

| 模块型号 | 能干嘛？ | 选型建议 | 对应要刷的底层文件 (在 `deploy/core/`) |
| :--- | :--- | :--- | :--- |
| **Air780EPM** | 只要收发短信 | **【最推荐买这个】**<br>最新二代芯片，发热小、价格便宜，做短信网关性价比最高（支持电信卡）。 | **`LuatOS-SoC_V2050_Air780EPM_103.soc`**<br>*(⚠️ 千万别选 109 固件，见下方避坑)* |
| **Air780EPV** | 短信 + 电话振铃 | **【全能款】**<br>支持所有网络（含电信），唯一支持用电话振铃提醒紧急事件。 | `LuatOS-SoC_V2001_EC718PV_CLOUD.soc` |
| **Air780E / EC** | 老款板子收短信 | **【手头有就继续用】**<br>经典老款开发板，不支持电信卡。插电脑上需按一下开机键。 | `LuatOS-SoC_V1124_EC618.soc` |

---

## 🚀 3 步快速上手

新手拿到模块后，照着下面 **3 步** 就能跑起来：

### 第 1 步：连电脑
- 用一根 **能传数据的 Type-C 线**（别用只能充电的两芯线）把模块插到电脑上；
- *注意：如果是 Air780E/EC 老款开发板，插上后长按板子上的 `POW` 键 2 秒开机（红灯亮）。*

### 第 2 步：一键刷固件
1. 电脑打开合宙官方下载工具 **Luatools**；
2. 点击右上角 **【项目管理测试】**，在左边直接选中我们做好的现成工程：
   - 780EPM 选 ➔ **`Air780EPM智能网关.ini`**
   - 780EPV 选 ➔ **`smart-gateway-780epv.ini`**
   - 780E/EC 选 ➔ **`Air780EC智能网关.ini`**
3. 点右下角的 **【下载底层和脚本】** 烧录进去（如果没反应，按住板子上的 BOOT 键再插一次电脑）。

### 第 3 步：插卡即用
1. 模块拔下来插上 SIM 卡，重新插回电脑；
2. 双击打开 **`tools/host_gateway/dist/Air780EPV-Gateway.exe`**；
3. 电脑会自动弹出管理网页（`http://127.0.0.1:17801`），信号连上后就能正常收发短信了！

---

## 🗺️ 进阶文档导航

日常使用看上面 3 步即可。如果有更深入的定制需求，可查阅对应文档：

* 📱 **想把短信推送到飞书、企业微信、钉钉或 Bark？** ➔ [📖 查看推送配置教程](specs/push-channels.md)
* 🛠️ **电脑搜不到端口、没信号或者指示灯不亮？** ➔ [🛠️ 查看常见排错指南](specs/troubleshooting.md)
* ⚡ **模块刷坏了怎么救砖？怎么改线插电自动开机？** ➔ [⚡ 查看强刷救砖与改线说明](specs/flashing-and-toolchain.md)
* 🔌 **模块各引脚定义、串口说明与 AT 指令？** ➔ [🔌 查看硬件详细规格](specs/hardware-spec.md)
* 🤖 **想对接 AI Agent (MCP) 或调用 HTTP API 自动发信？** ➔ [🔌 查看接口与 MCP 说明](specs/api-and-integration-guide.md)
* 🧠 **想研究低内存防死机设计与黑匣子存储原理？** ➔ [🏗️ 查看系统架构设计](specs/smart-gateway-architecture.md)

---

## ⚠️ 新手必看避坑

1. **780EPM 千万不要刷 `109` 固件，必须选 `103`**  
   合宙的 109 固件是纯随身 WiFi 专用的，**原厂直接把短信功能删掉了**。刷了 109 一插卡就会无限死机重启报错。请务必使用我们提供的 **`_103.soc`** 底层。
2. **底包不要跨型号乱刷**  
   老款 780E 只能用 V11xx 底包，新款 780EPM/EPV 只能用 V20xx 底包，两代芯片完全不一样，混刷会黑屏开不了机。
3. **老款开发板插上电脑没反应？**  
   老款 780E 开发板出厂默认是关机态，插上 USB 后需要**长按板子上的 POW 键 2 秒**才会开机。嫌每次开机麻烦的，把 POW 按键两端用焊锡短接就能通电自启。

---

## 📄 开源协议 (License)

本项目遵循 [MIT 开源协议](LICENSE)。欢迎提交 Issue 与 PR 共同完善！
