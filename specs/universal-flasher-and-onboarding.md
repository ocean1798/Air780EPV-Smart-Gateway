# 上位机多芯片通用固件线刷与全新模组接入向导方案 (AIR-35)
**版本**: v2.0 (独立方案审查修正版)  
**状态**: 审查已吸收 · 待确认实施  
**主要修订**: 固化静态刷机底座、补充全新模组 Full Flash 模式、收敛端口探测线程、健全 Bootloader 盲态防呆机制。

---

## 1. 目标与愿景

### 1.1 业务原目标
解决用户“拿到全新模块必须额外下载合宙 Luatools 工具才能首次烧录”的痛点。
在网关上位机（Web 控制台 / Windows 桌面版）内实现：
1. **脱离第三方工具依赖**：彻底不需要用户安装、打开或配置合宙 Luatools 软件；上位机自带经过严格测试的线刷引擎与经过验证的稳定基线内核；
2. **多芯片多型号自动识别与兼容**：全面兼容合宙主流 4G Cat.1 模组（Air780EPV、Air780E、Air780EG、Air700E、Air780EC 等），自动识别芯片平台（移芯 EC718PV、EC718P、EC618、EC716E）；
3. **全新模块一键开箱即用（Zero-Tooling Onboarding）**：全新出厂模块插上电脑后，上位机智能捕获、自动灌入“底层 LuatOS 内核 + 最新通用网关业务脚本”，一键全自动点亮上线。

---

## 2. 深度技术调研与物理事实判定

### 2.1 硬件模组与芯片平台矩阵

| 模组型号 | 核心芯片 (BSP) | 核心特性 | 脚本分区基地址 | SOTA 容器 Magic | 内置稳定内核版本 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Air780EPV** | 移芯 EC718PV | 全功能 (VoLTE/音频/摄像头/LCD) | `0x00324000` | `0xeac37218` | `LuatOS-SoC_V2001_EC718PV` |
| **Air780EP** | 移芯 EC718P | 标准 Cat.1 + VoLTE | `0x00324000` | `0xeac37218` | `LuatOS-SoC_V2001_EC718P` |
| **Air780E** | 移芯 EC618 | 经典数传 (超低功耗，不支持语音) | `0x0024D000` | `0xeaf18c16` | `LuatOS-SoC_V1113_EC618` |
| **Air780EG** | 移芯 EC618 | EC618 + 华大北斗 GPS 导航 | `0x0024D000` | `0xeaf18c16` | `LuatOS-SoC_V1113_EC618` |
| **Air700E** | 移芯 EC618 | 超小尺寸 4G 数传 | `0x0024D000` | `0xeaf18c16` | `LuatOS-SoC_V1113_EC618` |
| **Air780EC** | 移芯 EC716E | 超高性价比精简款 | `0x00324000` | `0xeac37218` | 移芯 EC7xx 平台 |

### 2.2 全新模块插入的三种物理状态与应对策略

1. **状态 1：出厂标准 AT 状态（90% 用户场景）**
   - **物理特征**：全新出厂模块内部固化的是原厂标准 AT 固件，插入电脑后枚举 3 个 USB CDC 虚拟串口（VID: `19D1`, PID: `0001`）；
   - **识别机制**：上位机主线程扫描未绑定卡槽的端口，下发无阻塞 `ATI` 或 `AT+CGMM`，**模块 1 秒内回显型号名称（如 `Air780E`）**，上位机 100% 自动精确判定型号；
   - **刷机策略**：**全量内核刷写（Full Flash）**。因为出厂 AT 固件没有 Lua 虚拟机，必须全量刷入 `luatos.binpkg`（底层内核）+ `script.bin`（业务脚本），耗时约 8~12 秒；
   - **触发方式**：上位机下发软复位指令 `AT+ECRST=delay,799` 自动切入 ROM Bootloader，全程免按键。
2. **状态 2：空芯片 / 物理变砖 / 用户按住 BOOT 键插入（10% 急救场景）**
   - **物理特征**：插入后没有 AT 端口，直接以单一的 ROM Bootloader 端口（VID: `17D1`, PID: `0001`，如 COM9）暴露在系统中；
   - **识别机制**：**严禁自动盲刷**（移芯全系芯片在 17D1 态下的 USB 特征一致，无法通过软件探知具体芯片）；
   - **交互策略**：页面捕获到 17D1 端口进入，弹出【硬件修复向导】，展示醒目的【硬件型号确认下拉框】（提供 Air780EPV / Air780E / Air780EC 等选项，由用户点选确认）；用户确认后，上位机调配对应平台的完整镜像进行底层急救刷机。
3. **状态 3：已运行网关旧版本状态（存量升级场景）**
   - **处理机制**：已在线卡槽支持两级操作：日常更新走秒级 SOTA（免切 Boot），底层异常或重装走卡片上的“重新刷机”。

---

## 3. 总体架构设计与工程化资产拓扑

### 3.1 固化静态刷机底座（零运行时解包依赖）
摒弃在运行时调用 7z 动态解包的脆弱设计，直接将移芯全系线刷底座固化为网关工程的一等公民资产：

```
tools/flasher/
├── bin/
│   ├── FlashToolCLI.exe         # 移芯原厂无界面物理烧录器 (支持 EC718/EC618/EC716)
│   ├── fcelf.exe                # 移芯镜像解包/重组工具
│   └── soc_tools.exe            # 静态打包与 SOTA 封装工具
└── targets/
    ├── ec718pv/                 # Air780EPV 专属底座
    │   ├── agentboot_usb.bin    # EC718 ROM 握手微内核
    │   ├── config_ec718pv.ini   # 烧录配置模板
    │   └── luatos.binpkg        # V2001 稳定全功能内核 (内含 AP/CP/BL 镜像)
    └── ec618/                   # Air780E / Air700E 专属底座
        ├── agentboot_usb.bin    # EC618 ROM 握手微内核
        ├── format_ec618.json    # 分区格式化定义
        ├── MergeRfTable_ec618.bin # 射频校准表 (保护天线性能)
        ├── config_ec618.ini     # 烧录配置模板
        └── luatos.binpkg        # V1113 稳定数传内核 (内含 AP/CP/BL 镜像)
```
- **单文件打包保障**：在 `tools/host_gateway/build_exe.py` 中将 `tools/flasher/` 加入 `--add-data`，确保 Windows 独立桌面 exe 分发版在脱机、无网络、无 Luatools 的全新纯净电脑上 100% 自包含可用。

---

## 4. 详细模块设计

### 4.1 端口探测收敛（复用 `DonglePoolManager`，杜绝多线程竞争）
- **绝不新增独立的扫描线程**，避免引发 Windows 串口 `[WinError 5] 拒绝访问`；
- 在现有 `gateway_hub.py` 的 `DonglePoolManager._sync_ports` 轮询逻辑中进行收敛扩展：
  1. 获取系统所有串口，排除当前已被 `DongleSession` 占用的串口；
  2. 针对未占用的移芯串口组（VID: `19D1`）：
     - 选取疑似控制口进行快速 `ATI` 探测（500ms 超时），若返回识别到的型号（如 `Air780E`），记录至 `unassigned_dongles` 字典；
  3. 针对未占用的 Bootloader 端口（VID: `17D1`）：
     - 记录至 `recovery_dongles` 字典，标记为“Boot 待救砖设备”；
  4. 通过 WebSocket / SSE 广播给前端 `unassigned_dongles_update` 事件。

### 4.2 双模式物理线刷引擎（`firmware_flasher.py`）
重构现有的 `flash_hardware_cli()`，支持以下签名与工作模式：

```python
def flash_hardware_cli(
    target_port: str,
    chip_type: str,            # "ec718pv" 或 "ec618"
    mode: str = "full",        # "full" (底层内核+脚本) 或 "script_only" (仅脚本)
    script_bin_path: Optional[str] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    watchdog_timeout: int = 30
) -> Dict[str, Any]:
```

1. **配置动态拼装**：
   - 根据 `chip_type` 自动指向 `tools/flasher/targets/<chip_type>/`；
   - 调用 `luadb_packer.py (chip_type=chip_type)` 生成目标芯片专属基地址的最新网关 `script.bin`；
   - 若 `mode == "full"`：执行 `FlashToolCLI.exe --cfgfile config.ini --port <boot_port> burnbatch`，全量刷写底层操作系统内核、射频校准表与业务代码；
   - 若 `mode == "script_only"`：仅刷写 `flexfile2`（`script.bin`），2 秒极速修复；
2. **软复位与异常降级**：
   - 若目标端口为正常 AT 口（19D1），下发 `AT+ECRST=delay,799` 触发平滑重启进 Bootloader；
   - 若 6 秒内未检测到 Boot 端口，推流提示用户手动按住 BOOT 键插拔；
3. **完成平滑复位**：
   - 下发 `sysreset` 指令让模组重启进入正常工作态，等待网关中枢秒级收编上线。

### 4.3 前端交互设计（UI/UX）

1. **现有卡片“重新刷机”弹窗解封**：
   - 解除对 EC618 的置灰封锁；
   - 弹窗内支持切换：
     - `[⚡ 极速脚本修复 (约2秒)]`（默认）
     - `[🛠️ 全量系统重装 (约10秒)]`（用于修复底层异常或重装官方内核）
   - 点击【开始刷机】，实时呈现流式终端与动态进度条。
2. **全新模组接入横幅与引导弹窗（New Device Banner & Wizard）**：
   - 当插入全新模块时，大盘顶部滑出温和提示横幅：
     > “💡 检测到新接入的 4G 模组硬件：**合宙 Air780E (COM10)**，尚未安装网关系统。[一键安装网关]”
   - 点击【一键安装网关】，弹出向导对话框：
     - **目标设备**：合宙 Air780E（COM10 · 已自动检测确认）
     - **预装系统**：Air780 智能随身通信网关固件 v1.2.9（通用版）
     - 点击 **【⚡ 开始一键装机】**；
     - 进度条（0% -> 100%）走完后自动提示“装机成功！模组正在重启初始化...”，横幅消失，大盘卡槽列表中立刻点亮全新卡槽！

---

## 5. 分阶段实施计划（Implementation Plan）

### 阶段一：建立 `tools/flasher/` 独立工程资产库与重构线刷引擎
- [ ] 1.1 在 `tools/flasher/` 下建立扁平化资产结构，固化 `FlashToolCLI.exe` 及 EC718PV、EC618 官方经过验证的稳定底层镜像（`luatos.binpkg`、`agentboot` 等）；
- [ ] 1.2 重构 `firmware_flasher.py`，支持 `chip_type` 与 `mode`（全量内核刷写 / 仅脚本更新），编写单元测试 `test_flasher_config_assembler.py` 验证各平台配置生成与参数合法性；
- [ ] 1.3 更新 `tools/host_gateway/build_exe.py`，将 `tools/flasher/` 纳入打包资源。

### 阶段二：网关中枢未分配端口嗅探与刷机 API 闭环
- [ ] 2.1 在 `DonglePoolManager._sync_ports` 中新增对非会话移芯端口的安全轻量探测（`ATI`），收集 `unassigned_dongles`；
- [ ] 2.2 在 `gateway_web.py` 中新增 `/api/flasher/unassigned` 接口与更新 `/api/control/flash`，支持指定端口与模式触发全量/脚本线刷；
- [ ] 2.3 编写自动化端到端测试，验证端口探测无争抢、无异常死锁。

### 阶段三：Web 前端接入向导与重刷功能打通
- [ ] 3.1 在 Web 前端解封“重新刷机”弹窗，提供极速修复与全量重装切换，解除置灰；
- [ ] 3.2 增加【全新模组接入向导】提示条与安装向导弹窗，实现开箱一键装机；
- [ ] 3.3 保持 `tools/cluster_gateway` 与 `tools/host_gateway` 的核心资产 100% 字节对齐。

### 阶段四：实机真实全流程验证与物证归档
- [ ] 4.1 对实机卡槽 1（Air780EPV）与卡槽 2（Air780E）分别进行真实的全量系统刷机与极速脚本刷机验证；
- [ ] 4.2 采集 Playwright 双端端到端截图与控制台流式推流日志作为证据；
- [ ] 4.3 运行全量自动化回归测试（40+ 项），验证黑板规范合规性。

---

## 6. 核心安全红线（防变砖机制）

1. **芯片平台强校验红线**：
   - 物理线刷前，上位机必须对端口下发 AT 探针进行二次确认，确认芯片真实硬件与所选固件架构一致；
   - 严禁将 EC718PV 固件写入 EC618，反之亦然；一旦检测到硬件型号冲突，硬性中止并报警。
2. **射频校准数据隔离保护**：
   - 移芯芯片的射频校准参数存储在 `MergeRfTable.bin` 分区中，烧录配置严格保持参数对齐，杜绝刷机导致 4G 信号衰减。
3. **已有会话物理隔离红线**：
   - 端口扫描绝不向已被网关接管的会话（卡槽 1 / 卡槽 2）发送任何未经授权的 AT 或复位指令，确保生产服务 100% 零中断。
