# Air780EPV 随身通信网关插件 (Silicon Emergence Cellular Plugin)

本工程是为《硅基涌现》（Agent_V3）研发的智能随身通信网关扩展插件，严格遵循 **Plugin Platform Architecture v4.0** 规范。

## 1. 当前候选能力 (v1.1.0)
* **📱 真实设备状态**：当前候选只展示由已授权、已映射串口设备返回的 Air780EPV 版本、构建标识、协议版本、信号和蜂窝网络状态；没有实时回执时显示未知或最近缓存，不使用演示状态冒充真机；
* **🔌 NDJSON 会话**：板端通信使用 `{type:"cmd",id,cmd,params}`、`{type:"res",id,code,msg,data}`、`{type:"event",event,data,ts}`，支持任意分片、UTF-8 跨块、1 秒 ping、3–30 秒协议离线判断和断线半帧丢弃；
* **🧭 连接恢复提示**：未完成映射、设备不可见、权限不足、身份不符或接口不支持时，工作区返回具体恢复动作并保留最近状态；
* **🛡️ 纯安全沙箱兼容**：通过受控 `@silicon/plugin-sdk` 使用设备和存储能力，无动态代码注入、串口路径或外部网络调用。

短信表单、历史记录、来电、通知领取和 FOTA 的协议/桥接代码保留在源码中作为后续宽切片 WIP；当前 manifest 不声明这些权限或工作区命令，生产 workspace 不创建内存待发队列。

## 2. 目录结构
* `manifest.json`：当前状态候选清单（仅声明 `hardware.serial`、`plugin.storage` 和 `refresh` 工作区指令）
* `index.mjs`：Worker 沙箱执行入口与 Surface DTO (`plugin.workspace.v2`) 格式化，保留 v1 兼容
* `gateway-protocol.mjs`：纯 NDJSON 解码、心跳、断线与短信单在途状态机
* `gateway-sdk-bridge.mjs`：通过宿主受控 devices SDK 接入设备；records/通知/短信接收逻辑保留为未启用的宽切片 WIP
* `package_plugin.py`：标准 `.se-plugin` ZIP 二进制包打包脚本

## 3. 安装与使用
1. 运行 `python package_plugin.py`，生成 `dist/cellular-gateway.se-plugin`；
2. 打开《硅基涌现》Web 界面，进入【设置 -> 插件】；
3. 上传 `cellular-gateway.se-plugin` 安装包，确认授权并启用；
4. 左侧导航栏点击【随身通信网关】工作区，即可查看真实设备状态；未映射或未授权时按页面提示完成设备设置后点击刷新。
