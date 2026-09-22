# 🔔 多渠道即时推送配置指南 (Push Channels)

本项目支持在收到短信、验证码或来电拦截事件时，通过上位机自动借用本地宽带（不耗费板卡 4G 蜂窝流量）向多个协同平台推送富文本通知。

---

## 1. 支持的推送渠道全景

| 推送渠道 | 消息形态 | 渠道特性与核心亮点 | 配置所需参数 |
| :--- | :--- | :--- | :--- |
| **飞书机器人 (Feishu / Lark)** | **富文本互动卡片**<br>(Interactive Card) | • 动态突出显示高亮验证码<br>• 内置一键复制验证码 Action 按钮<br>• 展示发件人、时间、模组电量与 4G 信号 | `feishu.url`: 飞书群自定义机器人的完整 Webhook 地址 |
| **钉钉机器人 (DingTalk)** | **Markdown 卡片消息** | • 格式排版清晰整洁<br>• 支持 PC 端与移动端即刻提醒与免打扰设置 | `dingtalk.url`: 钉钉群机器人的 Webhook 地址 |
| **企业微信机器人 (WeCom)** | **Markdown / 文本消息** | • 无缝融入工作群组与移动办公<br>• 毫秒级到达率与多端同步推送 | `wecom.url`: 企业微信内部群机器人的 Webhook 地址 |
| **Bark (iOS)** | **原生极速通知** | • iOS 端最优体验，极简纯净<br>• 支持通知铃声、角标、分组管理<br>• 点击通知自动将动态验证码复制到 iPhone 剪贴板 | `bark.url`: 形如 `https://api.day.app/YOUR_KEY/` 的推送地址 |
| **自定义通用 Webhook** | **HTTP POST JSON** | • 支持对接任意自建服务、Home Assistant、Server酱、PushPlus、Telegram Bot 等<br>• 携带结构化 JSON 数据载荷 | `webhook.url`: 目标接收端 API 的 HTTP/HTTPS 地址 |

---

## 2. 配置文件位置与启用方式

上位机配置文件路径为：
`tools/host_gateway/gateway_config.json`

*(如果文件不存在，可直接复制同目录下的 `gateway_config.example.json` 并重命名)*。

您可以在配置文件中同时启用多个推送渠道（支持多渠道并发推送）：

```json
{
  "system": {
    "auto_copy_otp": 1
  },
  "feishu": {
    "enable": 1,
    "url": "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_FEISHU_BOT_TOKEN",
    "secret": ""
  },
  "wecom": {
    "enable": 0,
    "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY"
  },
  "dingtalk": {
    "enable": 0,
    "url": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN",
    "secret": ""
  },
  "bark": {
    "enable": 0,
    "url": "https://api.day.app/YOUR_BARK_KEY/",
    "group": "Air780EPV",
    "sound": "minuet"
  },
  "webhook": {
    "enable": 0,
    "url": "http://127.0.0.1:8123/api/webhook/YOUR_HA_WEBHOOK_ID",
    "method": "POST",
    "headers": {
      "Content-Type": "application/json"
    }
  }
}
```

---

## 3. 各渠道具体配置步骤

### 3.1 飞书群自定义机器人
1. 在飞书群聊中点击右上角 **群设置 ➔ 机器人 ➔ 添加机器人 ➔ 自定义机器人**；
2. 机器人名称填写 `4G通信网关`，复制生成的 **Webhook 地址**；
3. 将地址填入 `gateway_config.json` 的 `feishu.url` 中，并将 `feishu.enable` 设为 `1`；
4. 收到短信时，飞书将自动渲染出包含发件人、运营商、验证码高亮大字与复制按钮的高保真交互卡片。

### 3.2 企业微信机器人
1. 在企业微信群聊中右键添加群机器人；
2. 复制生成的 Webhook 地址，填入 `wecom.url`，并将 `wecom.enable` 设为 `1`。

### 3.3 钉钉群自定义机器人
1. 在钉钉群设置中添加“自定义机器人”，安全设置建议选择“自定义关键词”，关键词填入 `验证码` 或 `短信`；
2. 复制 Webhook 地址填入 `dingtalk.url`，并将 `dingtalk.enable` 设为 `1`。

### 3.4 Bark (iOS 专用极速推送)
1. 在 iPhone 的 App Store 下载 **Bark** App；
2. 打开 Bark 复制你的专有 Key，拼接为 `https://api.day.app/你的Key/`；
3. 填入 `bark.url`，并将 `bark.enable` 设为 `1`；
4. 当短信包含验证码时，Bark 会将验证码作为跳转参数下发，点击推送横幅即可瞬间将验证码复制到手机剪贴板。

### 3.5 自定义通用 Webhook (对接 Home Assistant / 自建服务)
当设置 `webhook.enable = 1` 时，上位机会向指定的 URL 发送标准 HTTP POST 请求，JSON 负载格式如下：
```json
{
  "event": "sms_received",
  "from": "10010",
  "content": "【中国联通】您的验证码是 849201，请于 5 分钟内输入。",
  "code": "849201",
  "time": 1726700000,
  "slot_id": "slot_1",
  "model": "Air780EPM",
  "imei": "868926082396117",
  "csq": 26
}
```
可直接在 Home Assistant 的自动化中利用 Webhook Trigger 实现智能家居联动（例如：收到某快递短信时联动播报或亮灯）。

---

## 4. 0 流量保号防线 (Push ACK 机制)
当模组插在运行有上位机的电脑上时：
1. 模组收到短信后，优先通过 USB 虚拟串口向电脑上位机上报；
2. 电脑上位机借用本地光纤宽带/Wi-Fi 向飞书/微信服务器发送推送；
3. 上位机推送成功后，立即向模组回送 `PUSH_ACK` 回执；
4. 模组收到回执，确认宽带已成功代推，**自身无需激活 4G 蜂窝数据**，保持 0 流量待机；
5. 若板卡脱机插在充电头上，可按需在 Web 控制台启用板端数据通信进行独立推送。
