# Air780EPV 智能网关 GitHub 公开开源与无痕发布规范

[![Specification](https://img.shields.io/badge/Spec-Release%20Governance-blue)](../README.md)
[![Status](https://img.shields.io/badge/Status-Active%20%7C%20Production-brightgreen)](implementation-plan.md)
[![Tool](https://img.shields.io/badge/Tool-tools%2Fpublish__to__github.py-purple)](../tools/publish_to_github.py)

---

## 1. 背景与架构演进决策

本项目孵化于个人人机协同工作区（Agent_V4 私有大仓），随着通信底层能力、双分治上位机、防 OOM 游标及单文件桌面端等技术闭环，需将其独立发布为公网开源项目（GitHub Public Repository）。

在如何将私有大仓子模块公开发布至 GitHub 的架构选型中，经历并否决了两种常见反模式，最终确立了工业级的**“单源开发 + 瞬时推流 (Ephemeral Pipeline)”**架构：

### 1.1 否决的反模式 1：本地持久化副本目录 (`output/` 或平行文件夹)
* **模式描述**：在本地磁盘建立持久的 `output/Air780EPV-Smart-Gateway` 目录，日常手工或脚本复制文件过去再 commit。
* **否决原因**：
  1. **双重维护与代码漂移**：开发者在本地面对两份一模一样的代码，极易发生“在 output 改了代码导致原项目丢失修改”或“原项目修复后遗漏同步”的灾难；
  2. **磁盘空间浪费与污染**：打破工作区单源（Single Source of Truth）原则，导致项目结构臃肿。

### 1.2 否决的反模式 2：Git 原生 Subtree 直推 (`git subtree push`)
* **模式描述**：利用 `git subtree push --prefix=...` 直接将子目录虚拟分支推向 GitHub。
* **否决原因**：
  1. **历史提交隐私泄漏**：`git subtree` 会完整提取该目录自创立以来的**所有 Git Commit 历史**。由于早期孵化阶段的某些历史提交包含真实的测试手机号与飞书 Webhook，直接推送会导致任何人可通过 `git log -p` 还原出私有敏感凭据。

### 1.3 采纳的架构：瞬时发布管道 (Ephemeral Pipeline，借鉴 Google Copybara 模式)
* **核心理念**：
  1. **本地唯一定位**：本地磁盘**永远只有当前工程一份代码**，绝不建立持久的 `output/` 文件夹；
  2. **临时管道推流**：发布脚本在操作系统内存/临时目录（`%TEMP%`）中瞬时完成白名单提取、脱敏门禁扫描、Git 打包与远端推流；
  3. **推完即焚**：推送完成后立即自动自毁临时工作区，本地 0 冗余残留。

---

## 2. 瞬时发布流水线时序与架构图

```text
       ┌─────────────────────────────────────────────────────────┐
       │   Agent_V4 本地开发工程 (唯一代码真相源)                 │
       │   projects/02work/20260909-合宙780系列模组开发/          │
       └───────────────────────────┬─────────────────────────────┘
                                   │
                    python tools/publish_to_github.py
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 操作系统临时缓存空间 (%TEMP%/air780_release_xxxxxx)                      │
│                                                                        │
│ 1. [资产打包] 按照发布白名单 WHITELIST_ITEMS 过滤提取关键源文件        │
│ 2. [物理阻断] 剔除 200MB 抓包底包、构建缓存、可执行文件与私有敏感配置  │
│ 3. [安全门禁] 执行 Security Gate 正则自检 (手机号/Webhook/密钥/绝对路径)│
│    ├── 检测未通过 ➔ 立即中止并向终端红色告警，坚决不提交               │
│    └── 检测通过   ➔ 进入下一步                                         │
│ 4. [版本打标] 配置纯净 Git 用户信息，自动生成 release commit           │
│ 5. [远端推流] 调用 git push -u origin main 推送至 GitHub               │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                     推送完成 ➔ 自动销毁临时空间
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 交付产物：                                                             │
│ • GitHub 远端仓库：拥有全新、纯净、无泄漏风险的公开开源分支             │
│ • 本地开发环境：干净整洁，无任何冗余文件夹，继续保持单源开发           │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 白名单与黑名单物理隔离矩阵

发布脚本严格按照以下白名单与黑名单规则执行资产抽取，杜绝任何多余文件或隐私带入公开仓库：

### 3.1 明确允许发布的白名单 (`WHITELIST_ITEMS`)
| 类别 | 包含路径 / 文件 | 作用与说明 |
| :--- | :--- | :--- |
| **开源规范文件** | `README.md`, `LICENSE`, `CHANGELOG.md`, `requirements.txt`, `.gitignore` | 开源合规必备基础设施 |
| **权威设计规范** | `specs/`（包含架构图谱、协议指南、硬件差异对比等全套 Markdown） | 架构与二次开发指引 |
| **板端固件源码** | `deploy/core/*.soc`, `deploy/smart-gateway-780epv/` | 官方 SOC 底包与板载 LuatOS 微内核源码 |
| **生态平台插件** | `plugins/silicon-emergence-cellular/` | 《硅基涌现》官方扩展插件源码 |
| **上位机宿主中枢** | `tools/host_gateway/`（单卡稳定纯净版） | Python 中枢、Web 控制台与构建脚本 (集群版独立维护) |
| **AI 协同中枢** | `tools/mcp_server/` | FastMCP 7 大物理工具服务源码 |
| **FOTA 固件发布** | `tools/fota/publish_ota.py`, `tools/fota/version.json` | 92B 头部生成与空中更新流水线 |
| **配置模板** | `tools/qiniu_config.example.json`, `gateway_config.example.json` | 安全配置占位模板 |
| **免安装烧录** | `tools/Luatools/flash_smart_gateway.py` | 自动化 2.5 秒免按键烧录脚本 |
| **自动化测试套** | `test_air17_push_ack.py`, `test_sms_lazy_load_live.py`, `test_exe_live.py` 等 | 真机与端到端自测用例 |

### 3.2 绝对阻断的黑名单过滤规则 (`BLACKLIST`)
1. **二进制与临时产物**：
   - 排除任何 `.pyc`, `.log`, `.pkg`, `.pyz`, `.tmp`, `.bak`；
   - 排除任何 `__pycache__/`, `build/`, `dist/`；
   - 排除 `tools/Luatools/log/4gdiag/`（数十个 200MB 抓包二进制文件）；
   - 排除单文件应用 `Air780EPV-Gateway.exe`（27.27 MB，通过 GitHub Releases 独立分发，不进 Git 树）。
2. **商业版与集群模块隔离**：
   - 阻断多卡槽集群管理版 `tools/cluster_gateway/`（定位为商业级独立卡池方案，不进入公开仓库）；
   - 阻断大仓专用的跨项目联动测试脚本（如 `test_silicon_plugin.py`、`test_session_pool_live.py`）；
3. **私有敏感凭据与内部治理**：
   - 阻断真实的 `gateway_config.json`；
   - 阻断真实的 `qiniu_config.json`；
   - 阻断内部变更追踪记录 `changes/` 与项目黑板 `board.md`（内部工步不暴露）。

---

## 4. 通用脱敏安全门禁机制 (Security Gate)

发布管道内置 `run_security_gate()`，在向 Git 提交前，逐一扫描所有文本与代码文件。若命中以下通用模式（且不属于白名单占位符），**流水线立即中断并退出（Exit Code 1），杜绝人为疏忽导致的隐私泄露**：

```python
# 1. 通用大陆手机号码正则 (排除 13800138000 等标准官方占位号码)
PHONE_REGEX = re.compile(r'(?<!\d)(1[3-9]\d{9})(?!\d)')

# 2. 真实飞书机器人 Webhook UUID (排除 your-feishu-webhook-uuid)
FEISHU_HOOK_REGEX = re.compile(r'open\.feishu\.cn/open-apis/bot/v2/hook/([a-f0-9\-]{20,})', re.IGNORECASE)

# 3. 真实云存储 Bucket 域名 (排除 your-bucket 或 example 占位)
QINIU_BUCKET_REGEX = re.compile(r'([a-z0-9_\-]+\.(?:(?:hd-bkt|sabkt)\.(?:clouddn|gdipper)\.com|clouddn\.com|qiniucs\.com))', re.IGNORECASE)

# 4. 本地 Windows / Linux 宿主绝对路径
LOCAL_PATH_REGEX = re.compile(r'[A-Za-z]:[\\/](?:Users|home|root)[\\/]', re.IGNORECASE)

# 5. 疑似硬编码云端密钥 (SecretKey / Token)
RAW_SECRET_KEY_REGEX = re.compile(r'["\'](?:secret[_-]?key|sk)["\']\s*[:=]\s*["\']([A-Za-z0-9_\-]{30,})["\']', re.IGNORECASE)
```

---

## 5. 开发者使用与日常发布维护手册

脚本统一收拢在：`tools/publish_to_github.py`。

### 5.1 首次发布配置
在 GitHub 上创建新的空白仓库（如 `Air780EPV-Smart-Gateway`），然后在当前工程根目录下执行：
```bash
python tools/publish_to_github.py -r git@github.com:您的用户名/Air780EPV-Smart-Gateway.git "feat: 初始发布 Air780EPV 智能网关开源版本"
```
*参数说明*：
- `-r / --remote`：GitHub 仓库的 SSH 或 HTTPS 地址。首次输入后自动保存在 `tools/publish_config.json`（已被 git 忽略），后续无需重复输入。

### 5.2 日常开发与持续发布
日常所有 Bug 修复、功能迭代均在原工程内正常进行。当阶段性成果完成并通过回归测试后，**只需执行一条命令**：
```bash
python tools/publish_to_github.py "feat: 优化短信提码算法并支持广电卡"
```
脚本会自动完成：临时目录检出 ➔ 白名单同步 ➔ 脱敏门禁扫描 ➔ Git Commit ➔ Git Push ➔ 自动清理缓存。

### 5.3 辅助维护指令
- **仅做脱敏自检**（不发布任何代码）：
  ```bash
  python tools/publish_to_github.py --check-only
  ```
- **发布演练模式**（在临时环境完整打包并测试提交，但不实际 push）：
  ```bash
  python tools/publish_to_github.py --dry-run "test: 验证本次发布的变动"
  ```

---

## 6. GitHub Releases 桌面端免安装应用分发指引

为避免 27.27 MB 的单文件桌面程序 `Air780EPV-Gateway.exe` 污染 Git 仓库导致克隆缓慢，确立如下分发标准：

1. **源码走 Git**：Git 仓库仅承载源代码与必要小体积资产，保持仓库在 20MB 左右极致轻量；
2. **二进制走 Release**：
   - 当版本发生变更时，上位机本地编译生成 `tools/host_gateway/dist/Air780EPV-Gateway.exe`；
   - 将其压缩为 `Air780EPV-Gateway-vX.Y.Z.zip`；
   - 在 GitHub 仓库页面点击 **Releases** ➔ **Create a new release**，打上版本标签（如 `v1.2.5`），并将 zip 包作为二进制附件上传供用户直接下载。

---

## 8. GitHub 仓库页面 SEO 与元数据配置指南

为使项目在 GitHub 站内搜索、各大搜索引擎（Google / Bing / 百度）以及开发者社区中获得最大曝光与精准检索匹配，建议在 GitHub 仓库主页右侧 **About** 栏目进行如下元数据配置：

### 8.1 仓库一句话描述 (Description)
```text
⚡ 合宙 Air780EPV 智能通信网关 | 全网通 4G 短信转发与 0 话费秒挂 | 电信 SMS over IMS | 智能验证码(OTP)提取 | 0 流量保号 | AI Agent FastMCP 物理工具 | Windows 免安装独立桌面端
```
*(英文备选: All-in-one 4G Cat.1 cellular dongle & SMS forwarder powered by Air780EPV (EC718P-V) and LuatOS. Features SMS over IMS, zero-traffic standby, OTP extractor, FastMCP AI protocol, and standalone Windows desktop app.)*

### 8.2 核心检索标签 (Topics)
建议添加以下 15 个高权重检索标签（覆盖硬件型号、微内核、协议技术与应用场景）：
- `air780epv`
- `air780e`
- `luatos`
- `sms-forwarder`
- `iot-gateway`
- `cellular`
- `cat1`
- `ec718pv`
- `volte`
- `sms-over-ims`
- `otp-extractor`
- `fastmcp`
- `mcp-server`
- `dongle`
- `openluat`

### 8.3 社区健康文件 (Community Health Files)
在 `.github/` 下已配置标准的 Issue 与 PR 模板（`bug_report.md`、`feature_request.md`、`pull_request_template.md`），可直接提升 GitHub 仓库的健康度指标（Repository Health Score）与推荐流权重。

### 8.4 社区分发与外链推广参考 (Backlinks Strategy)
正式建仓后，可参考 [`specs/community-distribution-drafts.md`](community-distribution-drafts.md) 中已起草好的发帖与 PR 模板，在 V2EX、OpenLuat 官方论坛以及 `awesome-mcp-servers` / `awesome-iot` 等权威代码库中申请收录，获取高质量的初始关注度与外链权重。

