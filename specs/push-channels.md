# 🔔 手机推送配置教程 (飞书 / 微信 / 钉钉 / Bark)

收到短信或验证码时，电脑软件会自动借用电脑自身的宽带网络（**不消耗模块里的手机卡流量**），将短信转发推送到你的手机或办公软件上。

---

## 1. 支持哪些推送平台？

| 推送工具 | 推送效果 | 怎么用？ |
| :--- | :--- | :--- |
| **飞书机器人** | **交互卡片（最推荐）** | 短信排版最漂亮，验证码会大字高亮显示，并自带【一键复制】按钮。 |
| **企业微信机器人** | **工作群消息** | 直接推送到企业微信内部群，适合移动办公接收。 |
| **钉钉机器人** | **群消息卡片** | 格式整洁，支持在群内提醒。 |
| **Bark (苹果手机)** | **iPhone 原生系统通知** | 体验最干净。收到通知点击横幅，**验证码会自动复制到 iPhone 剪贴板**。 |
| **自定义 Webhook** | **标准 JSON 数据** | 适合发给自己的服务器、Home Assistant 或各类自建通知系统。 |

---

## 2. 怎么配置？

配置文件在：
`tools/host_gateway/gateway_config.json`

*(如果还没有这个文件，复制一份同目录下的 `gateway_config.example.json` 并重命名为 `gateway_config.json` 即可)*。

想开哪个就把对应项的 `enable` 改成 `1`，把 Webhook 链接填进去即可（支持同时开多个）：

```json
{
  "system": {
    "auto_copy_otp": 1
  },
  "feishu": {
    "enable": 1,
    "url": "https://open.feishu.cn/open-apis/bot/v2/hook/填入你的飞书机器人Token",
    "secret": ""
  },
  "wecom": {
    "enable": 0,
    "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=填入企业微信Key"
  },
  "dingtalk": {
    "enable": 0,
    "url": "https://oapi.dingtalk.com/robot/send?access_token=填入钉钉Token",
    "secret": ""
  },
  "bark": {
    "enable": 0,
    "url": "https://api.day.app/填入你的BarkKey/",
    "group": "Air780",
    "sound": "minuet"
  },
  "webhook": {
    "enable": 0,
    "url": "http://127.0.0.1:8123/api/webhook/填入HA接收地址",
    "method": "POST"
  }
}
```

---

## 3. 各平台详细设置方法

### 3.1 飞书群机器人
1. 打开飞书群聊，点击右上角 **群设置 ➔ 机器人 ➔ 添加机器人 ➔ 自定义机器人**；
2. 机器人名字写 `短信网关`，复制生成的 **Webhook 地址**；
3. 填进 `gateway_config.json` 的 `feishu.url`，并将 `feishu.enable` 改为 `1`，重启软件即可。

### 3.2 企业微信群机器人
1. 在企业微信群聊里右键点击，选择 **添加群机器人**；
2. 复制生成的 Webhook 地址，填入 `wecom.url`，并将 `wecom.enable` 改为 `1`。

### 3.3 钉钉群机器人
1. 在钉钉群设置中添加“自定义机器人”；
2. 安全设置选“自定义关键词”，关键词填入 `验证码` 或 `短信`；
3. 复制 Webhook 地址填入 `dingtalk.url`，并将 `dingtalk.enable` 改为 `1`。

### 3.4 Bark (苹果 iPhone 专属)
1. 在 iPhone 的 App Store 搜 **Bark** 下载安装；
2. 打开软件复制主界面的专属链接（形如 `https://api.day.app/你的Key/`）；
3. 填入 `bark.url`，并将 `bark.enable` 改为 `1`。收到带验证码的短信时，点一下通知横幅就会自动存进手机剪贴板。

### 3.5 自定义 Webhook (对接自建系统 / 智能家居)
开启 `webhook.enable = 1` 后，每收到一条短信，软件就会以标准 HTTP POST 往你的接口推一条 JSON 数据：
```json
{
  "event": "sms_received",
  "from": "10010",
  "content": "【中国联通】您的验证码是 849201，请于 5 分钟内输入。",
  "code": "849201",
  "time": 1726700000,
  "slot_id": "slot_1",
  "model": "Air780EPM",
  "csq": 26
}
```

---

## 4. 为什么插电脑不会耗费手机卡流量？
模块收到短信后，会优先把短信交给电脑；电脑上的软件直接走家里的宽带或者 Wi-Fi 推送到飞书/微信。  
推成功后电脑会告诉模块“已搞定”，模块自身就不会开 4G 数据上网，从而实现 **0 流量消耗**。只有拔下模块插在普通充电头上且需要脱机推送时，才需要在后台开启模块自身的 4G 流量。
