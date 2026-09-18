# 原单卡应用的受管入口

`gateway_hub.py`、`gateway_web.py`、`gateway_runtime.py` 和 `web/index.html` 是桌面与插件共用的业务源。`gateway_app.py` 是桌面壳；`gateway_managed.py` 是硅基涌现使用的 CPython 3.12 入口。多卡工程、固件与用户已有配置不在这个构建范围。

单卡唯一编辑源就是本Project的`tools/host_gateway`，没有在硅基产品中复制一份插件业务源码继续迭代。`cluster_gateway`是既有多卡产品分支，不能等同单卡最新版；适用修复按已采用规范逐项核对。同源构建包及其中的源码是固定制品，不是第二个维护入口。

## 宿主控制

宿主以每次运行独立的0700 `runDir` 为工作目录，用专属venv运行 `python -s -m gateway_managed`，由 `codeDir/PYTHONPATH` 加载原包模块，并通过 `PYTHONDONTWRITEBYTECODE=1` 禁止字节码写入。包目录保持只读用途，运行产物不应污染其完整性校验。通过 stdin/stdout 传 ≤64KiB 的 NDJSON；日志只走 stderr。initialize 包含：

- `identity`：workspaceId、pluginId、packageDigest、runtimeEpoch、runtimeId、ownerLeaseId、grantVersion。
- `nonce`、`host: "127.0.0.1"`、宿主预分配的 `port`、`codeDir`。
- 已创建的绝对 `dataDir`、`cacheDir`、`logDir`；`webBasePath` 以 `/` 开始、结束。
- `credentialFile`：仅运行用户可读的文件（Linux 0600），原值不会进入控制回显。
- `allowedPermissions`：当前授予与包声明的交集；缺省不允许网络通知。

initialize 只启动只读 Web；不实例化常规 Hub、不扫描/打开串口、不收设备事件、不执行通知或业务写入。ready 回显身份、nonce、host/port、basePath、businessVersion。宿主完成持久业务标记后才发送 `{type:"activate", identity, nonce, device:null|单个设备}`。匹配的首次 activate 启动同进程 Hub/Web 线程，重复只回相同 active；`device:null` 提供无板配置和历史。EOF 或 shutdown 关闭整个应用；应用不 detach、不自行重启。

每个 HTTP 请求都校验 `Authorization: Bearer <credential>`，包括完整前缀下的 `api/runtime-identity`。健康响应的 `phase` 是 prepared 或 active；它不表示设备在线。平台负责代理、当前授权、完整进程组与撤权；原应用不会创建另一个浏览器登录或页面票据。

设备只使用注入的 `path`。打开前、打开后和每次重连校验所选字符设备的 sysfs 身份：`identity.serialNumber/vid/pid/interface`；有 `identityManifestPath` 时还核对启动身份及主次设备号。绝不枚举其他串口作为替代。缺设备或身份不符保持离线，不能凭真实串口状态未知声称硬件验收。

## 数据与能力

受管配置只读写 dataDir 的 `gateway_config.json`，不回退读取固件目录或旧电脑秘密。短信/来电事件在 active 后将最近 100/50 条保存到 `gateway_history.json`；这是有界宿主缓存，不承诺保留所有板端黑匣子历史。未选择导入时，不扫描既有数据。桌面目录可通过 `GATEWAY_DATA_DIR`、`GATEWAY_CACHE_DIR`、`GATEWAY_LOG_DIR` 指定。

原 API 与 EventSource 从 runtime-config.js 的 basePath 派生；桌面默认 `/`。SSE 重连时重新读取状态/历史，无历史事件重放承诺。受管实例最多 8 个流，队列有界，超过 256KiB 的单事件不会推流。关闭页面不停止 Hub。

实时短信的数字`time`或`ts`、板载历史及旧宿主缓存的时间，在Web输出时统一为既有`YYYY-MM-DD HH:MM:SS`格式（沿宿主本地时区）；已有日期字符串保留。否则数字时间与历史日期字符串混排会使新短信沉底，不能仅凭收到SSE就判定自动更新可用。旧缓存读取只规范输出，不改写原文件；原页面按用户选择的最新/最早顺序显示。回归入口为`python -B -m unittest test_managed_runtime.SmsRealtimeTests -v`，使用合成TCP/HTTP/SSE与原HTML脚本，不发送真实短信。

`/api/calls` 返回真实缓存；无设备时状态离线、号码和短信计数显示未知。FOTA 控制尚未实现，界面禁用、POST 返回 501。配置保存分别回报服务落盘与 `board_synced`，不会把板端未响应包装为成功。通知测试与后台宽带代推需要 `network.outbound`；首批本地自证不执行真实消息外发。

`/api/status` 每次都经现有Hub发送只读`get_status`，不能因Web上次的离线标记跳过探测。Hub明确`online:false`才作为断开结果；超时或通信失败返回`online:null`及实际原因，页面显示“状态未确认”并禁用控制，仍允许后续刷新/心跳查询。成功回包恢复Web状态、缓存和SSE；未知状态不通过布尔转换覆盖Hub确认的物理连接标记。运行active、串口在线、状态请求成功是不同层，不能仅凭页面固定文案判断设备已拔出。

09起，HTTP状态、SSE首帧与增量广播共同使用规范状态快照，`number/version`及兼容`raw`取值一致。同一连接的缺省号码保留已知值；明确空或无效号码、明确断开、设备新连接/gateway_ready与新Backend会清理旧号，未知状态不能伪装成物理断开。来电数字`time/ts`与旧缓存输出复用短信时间标准；读取不改写旧历史。历史游标仅允许ASCII `h:[0-9]+:[0-9]+`，非法不下传，单批仍最多15条。

`system.auto_copy_otp`默认开启，与其他system字段一起保存、读回及Hub热重载；Hub仅在值为true/1/"1"时执行原自动复制路径。配置保存仍可能同步板端，不能以只想验证偏好为由向真板写配置。四项无设备回归入口为`python -B -m unittest test_managed_runtime.UpstreamParityTests -v`；其中系统剪贴板/通知为替身，Windows真实桌面行为单独验收。

## 同源构建与自证

构建入口是同目录 `build_native.py`。它收集原Web与四个共享业务文件、受管启动器，生成插件manifest，将Linux锁文件、校验过SHA-256的pyserial离线wheel、LICENSE和构建身份装入ZIP格式的 `.se-plugin`。Python解释器由硅基涌现宿主镜像提供，不打入此插件包；Windows EXE是使用 `--windows` 时由同批源码另外生成的产物。

Linux 只需 `requirements-linux.lock` 中的 pyserial 3.5 universal wheel；不会带入 Windows 托盘依赖。Windows 构建使用 `requirements-windows.lock` 的固定 wheel/hash，在专属 venv 离线安装：

```powershell
python -m venv .runtime/native-app-r1/windows-venv
.runtime/native-app-r1/windows-venv/Scripts/python.exe -m pip install --no-index --require-hashes --find-links .runtime/native-app-r1/windows-wheelhouse -r requirements-windows.lock
.runtime/native-app-r1/windows-venv/Scripts/python.exe -B build_native.py --output-dir .runtime/native-app-r1/new-batch --wheelhouse .runtime/native-app-r1/wheelhouse --windows
```

output-dir 必须尚不存在。`build_exe.py` 的实际 dist/work/spec 都指向本批目录；构建不覆盖旧 dist。构建返回 EXE 与 `.se-plugin`，`artifacts.json` 记录各自 hash，`build-info.json` 被两者共同携带。sourceRevision 是按排序的 `path + NUL + 文件SHA256 + LF` 汇总的完整构建源码集 SHA256，类型明确为 `sha256-source-set`；baseGitRevision 仅是当时根仓基线，workingTree=true 不冒充已提交业务源码。sharedSourceDigest 对上述四个共享业务文件做同样汇总。

候选目录编号（例如candidate-06）只标识构建批次，不是业务版本或Git标签。判断某包是否仍对应当前工作区，应逐项核对该批 `build-info.json` 的源码清单，并将实际包摘要与 `artifacts.json`、目标宿主已装 `packageDigest` 对齐；源码有变后不能继续沿用旧“最新”结论。具体UAT包路径、摘要、安装与应用镜像更新的区别，统一见消费方的[构建与检查两个插件](../../../../01base/20260703-硅基涌现/.worktrees/hardware-status-r1/docs/native-app-plugins.md#构建与检查两个插件)；此处不另维护实时部署列表。

运行工程测试：`python -B -m unittest test_managed_runtime -v`。WSL 应用真实 Linux 权限文件系统，例如给 `GATEWAY_TEST_OUTPUT` 指定本任务专属 `/tmp` 路径；不以放宽凭据权限修复 DrvFS 权限差异。测试只使用空配置、无板与明确 fixture，覆盖预备/激活/重复激活/错误 nonce/EOF、凭据、前缀、原 API/SSE、配置保留、真实事件处理、流上限及不扫描别板。

EXE 的 `--verify-web <绝对JSON输出路径>` 运行打包后的原 Web，读取根页面、runtime-config 与来电接口，记录内嵌身份和 HTML hash 后退出；它在桌面互斥、托盘、浏览器及 Hub 前执行，不打开设备。该模式是构建自证，不代替真实桌面交互、插件宿主、硬件、部署或独立验收。
