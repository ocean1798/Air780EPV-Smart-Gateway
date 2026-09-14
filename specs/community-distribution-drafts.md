# 🚀 外部社区发帖与开源生态分发草案 (Community Distribution Drafts)

本文件为项目首次开源发布后，向各技术社区、垂类论坛与 Awesome 列表进行推广、获取高质量反向链接与精准开发者的发布素材草案。在 GitHub 正式推流建仓后，可直接复制以下草案发布。

---

## 1. V2EX 社区发帖草案

- **建议节点**：`分享创造` / `程序员`
- **标题**：
  > 彻底解决合宙电信 4G 短信与扣费痛点：做了一个开源的 Air780EPV 智能随身通信网关（带 Windows 独立桌面端 + AI FastMCP）
- **正文草案**：
  ```markdown
  大家周末好！

  相信很多折腾过合宙 4G 模组（Air780E / Air700E）做短信转发的 V 友都踩过几个经典大坑：
  1. **电信卡死穴**：旧款模组没有 IMS 协议栈，电信卡直接瘫痪，死活收不到 4G 短信；
  2. **扣费防不胜防**：插在电脑上做随身短信棒，每次来短信板子还是自己走 4G HTTP 上报，套餐流量和话费莫名其妙被扣；
  3. **板载 OOM 崩溃**：MCU 堆内存只有 128KB 左右，Web 页面一刷新或者短信一多，板子直接挂死重启；
  4. **传统方案太糙**：要么是简陋的终端黑框，要么还要装庞大的 Python 环境，串口换个口就报找不到。

  为了彻底根治这些痛点，我基于合宙最新款 **Air780EPV**（移芯 EC718P-V 架构，原生支持 VoLTE / SMS over IMS）深度打造了一套**智能随身通信网关系统**，现已完整开源：

  ### 🌟 核心硬核特性
  - **四网通真秒收**：原生 IMS 栈，移动 / 联通 / 电信 / 广电全网通，秒收短信与 0 话费来电秒挂；
  - **宿主宽带代推优先 (0 蜂窝流量)**：连电脑时短信一律借用电脑本地 Wi-Fi/宽带代推（飞书/Bark/企微），并带 Push ACK 串口回执，手机卡流量消耗严格为 0 字节；断电/离线时自动降级 4G 自推；
  - **防 OOM 游标分页**：定制 `h:gen:offset` 游标懒加载，峰值 RAM 压制在 12KB 以内，随便刷新绝不重启；
  - **开箱即用 27MB 独立桌面端**：免安装 Python，系统托盘常驻 + 自动唤起 Edge 原生独立 App 窗口，验证码直写 Windows 剪贴板；
  - **串口热拔插自愈**：通过 `19D1:0001` + `x.6` 拓扑探测，端口任意漂移 1.5 秒无感自愈；
  - **AI Agent FastMCP Ready**：内置 7 大标准物理通信工具，可直接接入 Claude Desktop、Cursor 或本地 Agent。

  - **GitHub 仓库**：https://github.com/YOUR_USERNAME/Air780EPV-Smart-Gateway
  - **桌面客户端下载**：Releases 页面已提供单文件绿色压缩包（直接双击即可运行）

  欢迎各位玩板卡、做个人短信中枢或 AI 助手落地的朋友体验与提 Issue！
  ```

---

## 2. 合宙官方论坛 (OpenLuat 社区) 发帖草案

- **建议版块**：`4G Cat.1 讨论区` / `项目与经验分享`
- **标题**：
  > 【实战开源】基于 Air780EPV 的智能通信网关：攻克电信 SMS over IMS、0 流量 Push ACK 宽带代推与防 OOM 游标懒加载
- **正文草案**：
  ```markdown
  各位合宙的极客和开发者们大家好！

  在 LuatOS 社区里，短信转发一直是非常热门的方向。但在实际长期使用中，大家普遍面临三大核心工程挑战：
  1. 电信 4G 必须依赖 VoLTE / SMS over IMS 才能下发短信，旧款 EC618 方案无法适配；
  2. 板端资源极其有限，一旦在板载 LittleFS 存储数十条短信并用 Web 拉取，容易触发系统堆内存 OOM 导致看门狗重启；
  3. 模组作为 Dongle 接入主机时，缺乏可靠的让位与回执协议，导致本地明明有宽带依然消耗 SIM 卡蜂窝数据。

  本项目利用 **Air780EPV** 强大的 EC718P-V 架构，配合上位机设计了一整套闭环方案：

  ### 关键技术实现
  1. **SMS over IMS 底层适配**：充分调用 Air780EPV 的 VoLTE 栈，解决电信卡收信难题；
  2. **Push ACK 双向状态机**：板端收信后开启 5 秒定时器等待主机，上位机通过宽带推送完成后下发 `notify_ack [ok]` 注销定时器，真正做到 0 流量保号；
  3. **`h:gen:offset` 环形黑匣子游标**：板端单批只吐出 15 条且正文截断，前端通过 IntersectionObserver 监听触底拉取下一批，内存占用稳定在 12KB 以内；
  4. **自适应端口拓扑嗅探**：通过 Windows 注册表与设备拓扑特征，彻底解决 USB 拔插后端口号由 COM8 漂移至 COM16 的上位机断连痛点。

  全套 LuatOS 脚本、上位机控制中枢、FastMCP 驱动与打包工具均已开源：
  👉 GitHub: https://github.com/YOUR_USERNAME/Air780EPV-Smart-Gateway
  ```

---

## 3. Awesome 列表收录提交 PR 草案 (Awesome PR Template)

### 3.1 提交到 `awesome-mcp-servers`
- **目标文件**：`README.md`
- **收录位置**：`Hardware & IoT` 或 `Communication` 分类下
- **提交内容**：
  ```markdown
  - [Air780EPV Cellular Gateway](https://github.com/YOUR_USERNAME/Air780EPV-Smart-Gateway) - FastMCP server for 4G Cat.1 cellular dongle (Air780EPV/Air780E). Supports SMS sending/receiving, OTP extraction, 0-traffic broadband proxy, and hardware watchdog control.
  ```

### 3.2 提交到 `awesome-iot`
- **目标文件**：`README.md`
- **收录位置**：`Cellular / SMS Gateways` 分类下
- **提交内容**：
  ```markdown
  - [Air780EPV Smart Cellular Gateway](https://github.com/YOUR_USERNAME/Air780EPV-Smart-Gateway) - An industrial-grade, zero-traffic 4G Cat.1 IoT dongle gateway with LuatOS microkernel, SMS over IMS for China Telecom, and standalone Windows desktop app.
  ```
