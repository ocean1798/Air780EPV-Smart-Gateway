# 合宙 780 与 700 系列硬件通信事实

## 一、 当前实测真机基线 (Air780EPV · 二代旗舰)

- **模组型号**：Air780EPV (移芯 EC718P-V 平台，二代 Cortex-M4F)
- **设备串号 (IMEI)**：`868655072235213`
- **出厂固件**：`AirM2M_780EPV_V1003_LTE_QAT`
- **推荐底包**：`LuatOS-SoC_V2001_EC718PV_CLOUD.soc`
- **USB 硬件 ID**：`VID_19D1 & PID_0001`
- **串口真实物理拓扑与映射（实测验证）**：
  - `COM6` (MI_00 / MI_04)：底层 AP UniLog / Trace 二进制诊断日志通道。
  - `COM7` (MI_02)：Modem 出厂 AT 指令引擎通道（默认波特率 115200）。
  - **`COM8` (MI_06 / VCOM)**：**LuatOS `uart.VUART_0` (ID: 32) 用户虚拟串口**（必须拉高 DTR/RTS）。
  - `COM9` (Bootrom)：芯片硬复位或软重启后的 ROM Bootloader / AgentBoot 烧录专用端口。
- **核心特色**：全系列唯一支持 VoLTE 蜂窝语音拨号与紧急振铃挂断；集成 IMS 协议栈原生支持电信 4G 短信。

---

## 二、 当前实测真机基线 (Air780EPM · 二代性价比轻量)

- **模组型号**：Air780EPM (移芯 EC718P/PM 平台，二代精简 Cortex-M4F)
- **设备串号 (IMEI)**：`868926082396117`
- **芯片特征字**：`6bef6,8,4,5d,8,EC718PM`
- **推荐底包**：**`LuatOS-SoC_V2050_Air780EPM_103.soc`**（内置完整 `sms` 库，预留 448KB 最大脚本空间）
- **⚠️ 致命避坑**：**严禁使用 `_109.soc` 固件**！109 为随身 WiFi 剪裁版，原厂彻底剔除了 `sms` 库，插卡瞬间直接触发虚拟机空指针与看门狗无限重启死锁！
- **USB 硬件 ID**：`VID_19D1 & PID_0001`（BootROM 态为 `VID_17D1 & PID_0001`）
- **串口真实物理拓扑（与二代 718 架构对齐）**：
  - `COM6` (MI_04)：AP 诊断通道 (ap log port)
  - `COM7` (MI_02)：SoC 日志与 AT 通道 (soc log port)
  - **`COM8` (MI_06)**：**用户虚拟串口 `uart.VUART_0` (ID: 32)**，网关业务通信端口
- **供电与开机**：纯模组贴片形态，USB / 5V 供电上电即自动开机，功耗极低、发热极小。

---

## 三、 当前实测真机基线 (Air780E / Air780EC · 一代经典)

- **评估板型号**：`EVB_Air780X_V1.5`
- **模组型号**：Air780E / Air780EC (移芯 EC618 平台，一代 Cortex-M3)
- **设备串号 (IMEI)**：`861551056385727` / `861551056385833`
- **推荐底包**：`LuatOS-SoC_V1124_EC618.soc`（一代官方最新稳定标准版）
- **板载物理资源**：
  - `S1`：BOOT 强制下载按键（拉低引脚插 USB 进入光刻 BootROM 救砖）
  - `S2`：POW 开机按键（模组引脚 7 POWKEY）
  - `S3`：RESET 硬件复位按键
  - `NET_LED`：网络状态指示灯（硬件固定绑定 `GPIO27`）
- **开机机制与通电自启改线**：
  - 出厂机制：插上 USB 默认处于待机关机态，需长按 S2 键 1.5~2 秒启动；
  - 随身网关插电自启改装：引脚 7（POWKEY）内部自带 5.6k 限流电阻并上拉到 VBAT，直接接地安全可靠。只需**将 S2 两脚短接**或**焊接板载 R6 跳线焊盘接地**，即可实现插入充电头或电脑 USB 即刻开机。
- **固件边界**：一代 EC618 芯片无硬件 IMS 协议栈，无法解码电信 4G 短信；无 VoLTE 语音库；固件体系（`V11xx`）与二代 EC718（`V20xx`）绝对禁止混刷！

---

## 四、 常用 AT 指令速查

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

