# 合宙 780 与 700 系列硬件通信事实

## 一、 当前实测真机基线 (Air780EPV)

- **模组型号**：Air780EPV (移芯 EC718P-V 平台)
- **设备串号 (IMEI)**：`868655072235213`
- **出厂固件**：`AirM2M_780EPV_V1003_LTE_QAT`
- **USB 硬件 ID**：`VID_19D1 & PID_0001`
- **串口真实物理拓扑与映射（重大勘误与实测验证）**：
  - `COM6` (MI_00 / MI_04)：底层 AP UniLog / Trace 二进制诊断日志通道（速率固定，供原厂底层分析）。
  - `COM7` (MI_02)：Modem 出厂 AT 指令引擎通道（默认波特率 115200，支持标准 3GPP AT 指令交互）。
  - **`COM8` (MI_06 / VCOM)**：**LuatOS `uart.VUART_0` (ID: 32) 用户虚拟串口**！
    - 此端口才是 LuatOS 脚本环境唯一绑定的双向全双工交互端口；
    - **上位机驱动硬约束**：PC 上位机连接 `COM8` 时**必须显式拉高 `DTR` 与 `RTS`**（如 Python pyserial 中设置 `ser.dtr = True; ser.rts = True`），否则 USB CDC-ACM 缓冲区不会向下传递数据帧。
  - `COM9` (Bootrom)：芯片硬复位或软重启后的 ROM Bootloader / AgentBoot 烧录专用端口。
- **网络适配器与防偷跑策略 (实测验证)**：
  - 设备名称：`Remote NDIS based Internet Sharing Device` (MI_00)
  - MAC：`20:89:84:6A:96:AB`
  - 驱动特性：底层移芯 EC718PV 芯片在固件内集成了 `NetifUlPkgFastPath` 直通驱动，当执行 `mobile.config(mobile.CONF_USB_ETHERNET, 1)` 或 `3` 时，模组将蜂窝 WAN IP（如 `10.164.x.x`）直接透传分配给 PC 网卡，实现高速 4G 随身上网。
  - 默认策略：**开机默认彻底关闭**（`mobile.CONF_USB_ETHERNET = 0`），杜绝 Windows 联网服务偷跑蜂窝流量；仅在上位机明确下发指令时动态点亮。
- **实测资源消耗基线（生产级通信网关常驻）**：
  - **LittleFS Flash 存储**：总配额 128 KB，业务常驻占用 **29 KB**，空闲 **99 KB**（空闲率 77.3%）。
  - **Lua 虚拟机运行堆内存**：总配额 128 KB，业务与网络栈就绪后常驻占用 **53 KB**，空闲 **75 KB**（空闲率 58.6%），远离 128KB OOM 红线。
- **SIM 卡与网络状态 (实测)**：
  - SIM 卡：中国联通 (ICCID: `89860125801523307793`)
  - 注册状态：`+CEREG: 1` (已注册归属地 4G E-UTRAN 网络，MCC/MNC `46001`)
  - 信号质量：`+CSQ: 25, 0` (良好满格)
  - 蜂窝 IP：`10.164.145.240`

---

## 二、 常用 AT 指令速查

| 功能分类 | 常用指令 | 说明 |
| :--- | :--- | :--- |
| **基础查询** | `ATI` | 查询模块型号与固件版本 |
| | `AT+CGMR` | 查询版本固件号 |
| | `AT+CGSN` | 查询模组 IMEI |
| | `AT+CBC` | 查询板载供电电压 (毫伏) |
| **SIM 与网络** | `AT+CPIN?` | 查询 SIM 卡是否识别就绪 (`READY`) |
| | `AT+QCCID` 或 `AT+ICCID` | 查询 SIM 卡 ICCID 卡号 |
| | `AT+CSQ` | 查询 4G 信号质量 (0~31) |
| | `AT+CEREG?` | 查询 4G LTE 注册状态 (1 为已注册) |
| | `AT+COPS?` | 查询运营商信息 (如 46001 为联通，46000 为移动) |
| | `AT+CGPADDR=1` | 查询当前蜂窝网络分配的 IP 地址 |
| **短信控制** | `AT+CMGF=1` | 设置为文本模式 (TEXT Mode) |
| | `AT+CSCS="GSM"` | 设置短信字符集 |
| | `AT+CPMS?` | 查询当前短信存储区容量 |
| | `AT+CMGL="ALL"` | 列出所有收到的短信 |
| | `AT+CMGS="手机号"` | 发送短信 (输入内容后以 Ctrl+Z 结束) |
| **VoLTE 语音** | `ATD手机号;` | 拨打电话 (注意末尾分号) |
| | `ATA` | 接听电话 |
| | `ATH` | 挂断电话 |

---

## 三、 生产固件与工具链基线

- **生产底包**：`LuatOS-SoC_V2001_EC718PV_CLOUD.soc`
  - 架构：`LuatOS@EC718PV base 23.11 bsp V2001 32bit`
  - 特性：开启 `LUAT_USE_VOLTE`、`LUAT_USE_TTS_16K`、`LUAT_USE_INTER_AMR`、`LUAT_USE_HTTPSRV`、`LUAT_USE_SMS`、`LUAT_USE_FOTA`。
  - 物理与驱动边界（勘误更正）：
    - 原生支持 `mobile.CONF_USB_ETHERNET` RNDIS 随身上网功能；
    - 但受移芯硬件 FastPath 机制制约，开启 RNDIS 时主机数据直接桥接至基站，不流经板端内嵌 LwIP 输入处理，故主机无法直接访问板载内嵌 80 端口 Web 服务。管理控制台应依托宿主端（端口 17801）进行。
- **烧录基线**：
  - 工具：`Luatools v3.4.9`（通过 Win32 API 向控件 `-31959` 投递 `BM_CLICK` 实现 2.5 秒免交互静默烧录）。
  - 开源 `luatos-cli`：在 Windows USB 管道下因 EC718P AgentBoot 握手延时存在 `os error 121` 限制，详见 `specs/flashing-and-toolchain.md`。

