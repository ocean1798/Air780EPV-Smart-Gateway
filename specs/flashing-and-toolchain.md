# 合宙 780 系列模组烧录与自动化工具链事实

本文档沉淀合宙 4G Cat.1 模组（尤其是移芯 EC718P / Air780EPV）在开发与部署中的烧录机制、工具链演进、自动化驱动方案及网络热更新（FOTA）事实。

---

## 1. 烧录机制与协议底层事实

### 1.1 移芯平台（EC718P / EC718PV）的引导生命周期
1. **正常开机态**：
   - 模组枚举为 3 个 USB 虚拟串口（VID: `19D1`, PID: `0001`）：
     - `COM7`：AT / 控制命令通道
     - `COM6`：AP 运行日志 / 调试通道
     - `COM8`：CP / Trace 核心日志通道
2. **下载引导态（ROM Bootloader）**：
   - 触发方式：向运行态发送软复位指令序列，或在硬件上拉低/拉高 BOOT 引脚后开机。
   - 模组重新枚举为单个下载专用串口（`COM9`，VID: `17D1`, PID: `0001`）。
   - **两阶段引导流程**：
     - **Stage 1 (ROM BL)**：PC 烧录工具连接 `COM9`，向芯片 SRAM 写入微型载荷 `agentboot.bin`（约 40KB）。
     - **Stage 2 (AgentBoot)**：芯片控制权交由 AgentBoot，重新初始化 Flash 控制器并切换通信时序，接收固件分卷或脚本二进制包（`script.bin`）并写入对应 Flash 扇区（如脚本区基址 `0x00324000`）。
     - **Stage 3 (Reboot)**：烧录完成后执行软复位，重新回到三串口运行态。

---

## 2. 烧录工具链实测对比与瓶颈

### 2.1 纯命令行开源工具：`luatos-cli` (v1.11.0)
- **实测表现**：
  - 自动检测并向模组发送软重启指令切入下载模式：**完全成功**（模组自动从 COM7 切到 COM9，无需手工按键）。
  - `agentboot` 引导载荷灌入：**完全成功**。
  - 写入脚本 Flash：**失败**，报错 `Error: Serial write failed: 信号灯超时时间已到 (os error 121)`。
- **底层原因与社区现状**：
  - 移芯 EC718P 原厂在进入 AgentBoot 后需要重新协商波特率或重置 USB CDC FIFO 缓冲区。
  - Windows 原生 USB 串行驱动在高速写入时若未收到正确的流控/握手确认，会直接抛出 `ERROR_SEM_TIMEOUT (121)`。
  - 官方 GitHub PR #2（*feat(ec718): support binpkg flashing over USB and UART1*）目前仍在修复并合并该 USB 握手问题。现阶段纯 USB 模式的 `luatos-cli` 暂不具备独立生产可用性。

### 2.2 官方 `Luatools` 的 MCP / AI 架构演进
- **v3.2.4 废弃架构**：
  - 曾短暂提供本地监听 `127.0.0.1:38380` 的 HTTP / MCP 服务，用于支持外部 Agent 调用 `project.flash`。
  - 因端口占用、冲突和频繁闪退等严重问题，官方在新版本中已将该服务彻底移除。
- **v3.3+ ~ 现行版架构（AirMaster）**：
  - 官方反向收敛：**Luatools 本身作为主界面客户端**，将 AI 助手直接内嵌在软件右下角（AirMaster@LuaTools）。
  - 内部 `ai_tools` 将烧录引擎作为进程内沙箱工具运行（`start_download`），外部不再暴露开放的网络端口。

---

## 3. 生产级免人工自动化烧录方案（Win32 消息触发）

为了彻底摆脱人工手动点击鼠标或物理按键，经实测验证，可使用 Windows 原生 Win32 API 向常驻的 `Luatools` 窗体发送消息：

### 3.1 核心技术原理
- `Luatools` 主窗体控件为标准 Win32 原生句柄（wxWidgets 实现）。
- 主界面快捷烧录区中的 **【仅下载脚本】** 按钮具有固定的控件 ID：`-31959`（Unicode 文本：`仅下载脚本`）。
- 向该按钮句柄直接投递 `BM_CLICK`（`0x00F5`）消息：
  1. `Luatools` 收到消息后立即在 2ms 内完成 Lua 脚本编译打包；
  2. 调度原厂闭源的 `FlashToolCLI.exe` 发送软复位帧；
  3. 模组自动软重启进入 COM9；
  4. 2.5 秒内完成 Flash 擦写；
  5. 自动软复位进入运行模式并输出启动日志。

### 3.2 自动化脚本实现范例
```python
import win32gui
import win32con
import ctypes

def auto_flash_script():
    main_hwnd = None
    def enum_cb(hwnd, extra):
        nonlocal main_hwnd
        if win32gui.IsWindowVisible(hwnd) and "Luatools" in win32gui.GetWindowText(hwnd) and "当前项目" in win32gui.GetWindowText(hwnd):
            main_hwnd = hwnd
    win32gui.EnumWindows(enum_cb, None)
    if not main_hwnd:
        raise RuntimeError("未检测到运行中的 Luatools 窗体")

    btn_hwnd = None
    def find_btn(chwnd, extra):
        nonlocal btn_hwnd
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(chwnd, buf, 512)
        if buf.value == "仅下载脚本":
            btn_hwnd = chwnd
    win32gui.EnumChildWindows(main_hwnd, find_btn, None)
    if not btn_hwnd:
        raise RuntimeError("未找到'仅下载脚本'按钮")

    # 触发静默下载，全程无需鼠标或物理按键
    win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
```

### 3.3 Luatools 工程配置 INI 编码陷阱（关键排查事实）
- **现象**：外部脚本自动生成或修改 `tools/Luatools/project/*.ini` 后，触发烧录时 Luatools 报错 `底层core文件不存在!!` 且无法启动下载。
- **底层根因**：Luatools（底层基于 Python 2/3 Windows C API 与 wxWidgets 构建）在解析 `.ini` 格式时，**强制假定为 Windows 原生 ANSI (GB18030 / CP936) 编码**。如果外部程序以现代通用的 `UTF-8`（无 BOM）保存了包含中文字符的目录路径（例如路径包含 `合宙780系列模组开发`），文件中的中文字节会被当作乱码处理，导致 `core_path` 与脚本目录解析失败。
- **固化规范**：自动化写入或动态更新 Luatools 项目 `.ini` 配置文件时，在 Python 中必须显式指定 `encoding="gb18030"`，严禁以纯 UTF-8 覆盖保存。

---

## 4. 无感长效热更新机制（OTA / FOTA）

对于长期通电运行或免插拔 USB 的设备，代码级迭代推荐采用 LuatOS 内置的 FOTA 机制：
1. **板端 HTTP / TCP 接收**：
   - 当前 Air780EPV 固件已内置 `LUAT_USE_HTTPSRV` 与 HTTP 客户端。
   - 可在板端暴露轻量更新接口或定时向服务器请求最新 `script.bin`。
2. **本地 Flash 写入与热启**：
   - 下载新脚本包后，调用 `fota.file("/script.bin")` 将固件写入 Flash 升级分区。
   - 调用 `rtos.reboot()`，硬件引导程序自动解包覆盖并在 3 秒内启动最新业务代码。
   - 该方案可彻底摆脱物理 Type-C 与上位机烧录工具依赖。
