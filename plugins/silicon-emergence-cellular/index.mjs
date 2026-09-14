import { devices, records, storage } from "@silicon/plugin-sdk";
import { createGatewaySdkBridge } from "./gateway-sdk-bridge.mjs";

const SNAPSHOT_KEY = "cellular_gateway_snapshot";
// The representative package is intentionally status-only until the real
// device/records/notification grants are accepted together. The wider SMS
// surface remains below as compatibility WIP, but is not reachable from this
// candidate manifest and cannot create an in-memory production queue.
const STATUS_SLICE_ONLY = true;
const OPERATION_STATES = new Map();
const PROTOCOL_EVENTS = [];
let latestGatewaySnapshot = null;
let gatewaySnapshotLive = false;
let gatewayConnectionIssue = null;
let gatewayConnectPromise = null;

const gatewayBridge = createGatewaySdkBridge({
  devices,
  records,
  restoreOperationsOnConnect: false,
  onEvent(event) {
    if (event?.type === "gateway.status") {
      const status = event.state;
      if (["OPEN", "READY", "RECOVERED"].includes(status)) {
        gatewayConnectionIssue = null;
      } else if (["DISCONNECTED", "ERROR", "RECONNECT_WAITING"].includes(status)) {
        gatewayConnectionIssue = connectionIssueFor(event.error, "gateway_device_unavailable");
      }
    }
    if (PROTOCOL_EVENTS.length >= 64) PROTOCOL_EVENTS.shift();
    PROTOCOL_EVENTS.push(event);
  },
  onOperation(operation) {
    OPERATION_STATES.set(operation.operationId, operation);
  },
  onSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return;
    gatewaySnapshotLive = true;
    latestGatewaySnapshot = { ...(latestGatewaySnapshot ?? {}), ...snapshot };
  },
});

function displayValue(value, fallback = "未知") {
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

function connectionIssueFor(error, fallbackCode = "gateway_device_unavailable") {
  const code = typeof error?.code === "string" ? error.code : fallbackCode;
  const messages = {
    gateway_device_selection_required: "没有唯一的已映射串口设备，请在插件详情选择并启用 Air780EPV 设备。",
    gateway_device_not_found: "已映射设备当前不可见，请检查设备插入状态和授权后点击刷新。",
    gateway_device_identity_mismatch: "设备身份与当前映射不符，请在插件详情重新选择已授权设备。",
    plugin_permission_denied: "插件未获串口设备访问授权，请在插件详情允许硬件串口后点击刷新。",
    hardware_device_permission_denied: "插件未获串口设备访问授权，请在插件详情允许硬件串口后点击刷新。",
    hardware_device_mapping_invalid: "设备映射无效或已停用，请在插件详情重新启用 Air780EPV 设备。",
    hardware_device_unsupported: "当前串口接口不支持 Air780EPV 协议，请确认映射到正确接口。",
    hardware_device_interface_unsupported: "当前串口接口不支持 Air780EPV 协议，请确认映射到正确接口。",
    gateway_device_unsupported: "当前设备接口不支持此插件，请确认映射到正确接口。",
    hardware_device_unavailable: "设备接口暂不可用，请检查设备连接后点击刷新。",
    gateway_device_unavailable: "设备连接尚未完成，请在插件详情检查映射与授权后点击刷新。",
  };
  return Object.freeze({ code, message: messages[code] ?? messages.gateway_device_unavailable });
}

function workspaceIssue(code, message) {
  return Object.freeze({ code, message });
}

function operationList(operations) {
  return (Array.isArray(operations) ? operations : []).filter((operation) => operation?.operationId).slice(-32);
}

function formatStatusWorkspaceV2(snapshot, operations, surfaceIssue) {
  const hw = snapshot?.hardware || {};
  const net = snapshot?.cellular_network || {};
  const sys = snapshot?.system_status || {};
  const networkReady = net.network_ready === true;
  const networkUnknown = typeof net.network_ready !== "boolean";
  const cachedLabel = gatewaySnapshotLive ? "实时" : "最近缓存";
  const observedText = networkReady
    ? `网关在线 · 信号 CSQ ${displayValue(net.csq_signal)} · 蜂窝网络 ${displayValue(net.network_ready)} `
    : networkUnknown ? "网关状态未知，等待设备握手" : "网关正在搜网或离线";
  const statusText = surfaceIssue ? `连接失败：${surfaceIssue.message}` : observedText;
  const tone = surfaceIssue ? "warning" : networkReady ? "success" : "neutral";
  const blocks = [{
    type: "text",
    text: `状态采样（${cachedLabel}）：${statusText} · 模组 ${displayValue(hw.model)} · 固件版本 ${displayValue(snapshot?.version)} · 构建 ${displayValue(snapshot?.build_id)} · 协议 ${displayValue(snapshot?.protocol_version)} · 信号 ${displayValue(net.csq_signal)} · 运行 ${displayValue(sys.continuous_uptime)}`,
  }];
  if (surfaceIssue) {
    blocks.push({
      type: "text",
      text: `恢复动作：${surfaceIssue.message} 点击“刷新”重新枚举并握手。上次状态仍保留供核对。`,
    });
  }
  return {
    schema: "plugin.workspace.v2",
    status: { tone, text: statusText },
    blocks,
    actions: [{ commandId: "refresh" }],
    operations: operationList(operations),
  };
}

function operationSnapshot() {
  return [...OPERATION_STATES.values()].slice(-32).map((operation) => ({ ...operation }));
}

function mergeGatewaySnapshot(cachedSnapshot) {
  if (!latestGatewaySnapshot) return cachedSnapshot;
  return {
    ...(cachedSnapshot && typeof cachedSnapshot === "object" && !Array.isArray(cachedSnapshot) ? cachedSnapshot : {}),
    ...latestGatewaySnapshot,
  };
}

function historyItemFromRecord(record) {
  const payload = record?.payload && typeof record.payload === "object" && !Array.isArray(record.payload)
    ? record.payload
    : {};
  return {
    id: payload.id ?? record?.recordId ?? null,
    sender: payload.from ?? payload.sender ?? payload.phone ?? null,
    time: payload.time ?? payload.timestamp ?? record?.occurredAt ?? null,
    content: payload.content ?? null,
    otp: payload.code ?? payload.otp ?? null,
  };
}

async function readStoredSmsHistory(options = {}) {
  const listed = await gatewayBridge.listRecords({
    limit: Number.isSafeInteger(options.limit) ? options.limit : 50,
    ...(options.cursor ? { cursor: options.cursor } : {}),
  });
  return {
    items: Array.isArray(listed?.items)
      ? listed.items
        .filter((record) => record?.type === "sms.received" || record?.type === "sms.history")
        .map(historyItemFromRecord)
      : [],
    nextCursor: listed?.nextCursor ?? null,
  };
}

function formatWorkspaceV2(snapshot, operations = operationSnapshot(), surfaceIssue = gatewayConnectionIssue) {
  if (STATUS_SLICE_ONLY) return formatStatusWorkspaceV2(snapshot, operations, surfaceIssue);
  const hw = snapshot?.hardware || {};
  const net = snapshot?.cellular_network || {};
  const sys = snapshot?.system_status || {};
  const latestSms = snapshot?.latest_sms || null;
  const callRecords = Array.isArray(snapshot?.call_records || snapshot?.calls) ? (snapshot.call_records || snapshot.calls) : [];
  const smsHistory = Array.isArray(snapshot?.sms_history || snapshot?.recent_sms) ? (snapshot.sms_history || snapshot.recent_sms) : [];
  const networkReady = net.network_ready === true;
  const networkUnknown = typeof net.network_ready !== "boolean";
  const tone = networkReady ? "success" : networkUnknown ? "neutral" : "warning";
  const statusText = networkReady
    ? `网关在线 · 信号 CSQ ${displayValue(net.csq_signal)} · 供电 ${displayValue(hw.voltage_vbat)}`
    : networkUnknown ? "网关状态未知，等待设备握手" : "网关正在搜网或离线";
  const blocks = [
    {
      type: "text",
      text: `状态采样：${statusText} · 模组 ${displayValue(hw.model)} · 温度 ${displayValue(hw.temperature_celsius)} · 运行 ${displayValue(sys.continuous_uptime)} · 固件 ${displayValue(snapshot?.build_id ?? snapshot?.version)} · FOTA ${displayValue(snapshot?.capabilities?.fota)}`,
    },
    {
      type: "form",
      formId: "send-sms",
      title: "发送短信",
      fields: [
        { fieldId: "phone", type: "text", label: "完整号码", value: displayValue(snapshot?.compose_phone, ""), required: true, placeholder: "请输入完整号码，例如 +8613800138000" },
        { fieldId: "content", type: "textarea", label: "短信内容", value: displayValue(snapshot?.compose_content, ""), required: true, placeholder: "请输入短信内容" },
      ],
      submitCommandId: "send_sms",
    },
  ];
  if (latestSms) {
    blocks.push({ type: "text", text: `最新短信：${displayValue(latestSms.sender || latestSms.phone)} · ${displayValue(latestSms.time)} · ${displayValue(latestSms.otp ? `验证码 ${latestSms.otp}` : "无验证码")}` });
    blocks.push({ type: "text", text: `正文：${displayValue(latestSms.content, "暂无正文")}` });
  } else {
    blocks.push({ type: "text", text: "最新短信：暂无新短信或验证码" });
  }
  if (callRecords.length) {
    blocks.push({
      type: "table",
      tableId: "call-records",
      title: "来电记录",
      columns: [{ key: "recordId", label: "记录" }, { key: "sender", label: "号码" }, { key: "time", label: "时间" }, { key: "state", label: "状态" }],
      rows: callRecords.slice(0, 50).map((record, index) => ({ recordId: displayValue(record.id, `call-${index + 1}`), sender: displayValue(record.sender || record.phone), time: displayValue(record.time), state: displayValue(record.status, "已记录") })),
      pagination: { page: 1, pageSize: 50, total: callRecords.length, nextCursor: callRecords.length > 50 ? "calls:2" : null },
    });
  }
  if (smsHistory.length) {
    blocks.push({
      type: "table",
      tableId: "sms-history",
      title: "短信历史",
      columns: [{ key: "recordId", label: "记录" }, { key: "sender", label: "来源" }, { key: "time", label: "时间" }, { key: "content", label: "内容" }],
      rows: smsHistory.slice(0, 50).map((record, index) => ({ recordId: displayValue(record.id, `sms-${index + 1}`), sender: displayValue(record.sender || record.phone), time: displayValue(record.time), content: displayValue(record.content, "暂无正文") })),
      pagination: { page: 1, pageSize: 50, total: smsHistory.length, nextCursor: smsHistory.length > 50 ? "sms:2" : null },
    });
  }
  return {
    schema: "plugin.workspace.v2",
    status: { tone, text: statusText },
    blocks,
    actions: [{ commandId: "refresh" }, { commandId: "send_sms" }, { commandId: "get_history" }, { commandId: "fetch_otp" }],
    operations: operations.filter((operation) => operation && operation.operationId).slice(-32),
  };
}

function formatStatusCard(snapshot, surfaceIssue = gatewayConnectionIssue) {
  if (STATUS_SLICE_ONLY) {
    const { operations: ignoredOperations, ...legacySurface } = formatStatusWorkspaceV2(snapshot, [], surfaceIssue);
    return { ...legacySurface, schema: "plugin.workspace.v1" };
  }
  if (!snapshot) {
    return {
      schema: "plugin.workspace.v1",
      status: {
        tone: "warning",
        text: "4G 随身网关未连接 / 等待同步",
      },
      blocks: [
        {
          type: "text",
          text: "尚未接收到 Air780EPV 智能网关数据快照。请确认网关中枢与同步桥接服务已启动并在同一环境运行。",
        },
        {
          type: "text",
          text: "提示：网关出厂默认运行在 0 流量纯信令保号模式下，电脑宽带代推通知且绝不消耗手机卡蜂窝流量。",
        },
      ],
      actions: [
        { commandId: "refresh" },
      ],
    };
  }

  const phone = snapshot.phone || "未知";
  const hw = snapshot.hardware || {};
  const net = snapshot.cellular_network || {};
  const sys = snapshot.system_status || {};
  const latestSms = snapshot.latest_sms || null;
  const callRecords = snapshot.call_records || snapshot.calls || [];
  const smsHistory = snapshot.sms_history || snapshot.recent_sms || [];

  const tone = net.network_ready === true ? "success" : typeof net.network_ready === "boolean" ? "warning" : "neutral";
  const statusText = net.network_ready === true
    ? `4G 随身网关在线 (信号: CSQ ${net.csq_signal ?? "--"} | 供电: ${hw.voltage_vbat ?? "--"})`
    : typeof net.network_ready === "boolean" ? "4G 蜂窝搜网中..." : "网关状态未知 / 等待设备握手";

  const blocks = [
    {
      type: "text",
      text: `【📱 本机卡号与硬件看板】本机号码: ${phone} | 模组: ${hw.model ?? "未知"} | 供电: ${hw.voltage_vbat ?? "未知"} | 温度: ${hw.temperature_celsius ?? "未知"}℃ | 连续运行: ${sys.continuous_uptime ?? "未知"}`,
    },
    {
      type: "text",
      text: `【🌐 网络控制中心】蜂窝移动数据: ${sys.cellular_data_mode ?? "未知"} | USB 网络共享: ${sys.rndis_internet_sharing ?? "未知"} | 黑匣子存档: ${sys.blackbox_sms_count ?? "未知"} 条`,
    },
  ];

  // 3. 来电拦截记录卡片
  if (Array.isArray(callRecords) && callRecords.length > 0) {
    const callItems = callRecords.slice(0, 5).map((c) => {
      const timeStr = c.time || "--";
      const sender = c.sender || c.phone || "未知来电";
      const duration = c.duration ? `(振铃 ${c.duration}s)` : "";
      return `[${timeStr}] 来电号码: ${sender} ${duration} ➔ 拦截状态: 已秒级拒接 (0话费)`;
    });
    blocks.push({
      type: "text",
      text: `【📞 来电拦截记录】共捕获 ${callRecords.length} 条来电，展示最新 ${callItems.length} 条:`,
    });
    blocks.push({
      type: "list",
      items: callItems,
    });
  } else {
    blocks.push({
      type: "text",
      text: "【📞 来电拦截】暂无来电记录 (智能拦截引擎实时守候中，捕获振铃即秒级拒接，杜绝骚扰与扣费)",
    });
  }

  // 4. 最新短信与动态验证码高亮
  if (latestSms) {
    const otpTag = latestSms.otp ? ` [🔥 动态验证码: ${latestSms.otp}]` : "";
    blocks.push({
      type: "text",
      text: `【💬 最新短信${otpTag}】发件人: ${latestSms.sender ?? "--"} (接收时间: ${latestSms.time ?? "--"})`,
    });
    blocks.push({
      type: "text",
      text: `正文内容: ${latestSms.content ?? "--"}`,
    });
  } else {
    blocks.push({
      type: "text",
      text: "【💬 短信黑匣子】当前暂未收到新短信或验证码。",
    });
  }

  // 5. 历史短信存档列表
  if (Array.isArray(smsHistory) && smsHistory.length > 0) {
    const historyItems = smsHistory.slice(0, 5).map((s) => {
      const timeStr = s.time || "--";
      const sender = s.sender || s.phone || "未知";
      const otpStr = s.otp ? ` [验证码: ${s.otp}]` : "";
      const content = s.content || "";
      const truncated = content.length > 60 ? `${content.slice(0, 57)}...` : content;
      return `[${timeStr}] ${sender}${otpStr}: ${truncated}`;
    });
    blocks.push({
      type: "text",
      text: `【📜 短信黑匣子存档】展示最近 ${historyItems.length} 条记录:`,
    });
    blocks.push({
      type: "list",
      items: historyItems,
    });
  }

  return {
    schema: "plugin.workspace.v1",
    status: {
      tone,
      text: statusText,
    },
    blocks,
    actions: [
      { commandId: "refresh" },
      { commandId: "fetch_otp" },
    ],
  };
}

function requestStatusSnapshot() {
  if (!gatewayBridge.state().sessionId) return false;
  try {
    gatewayBridge.command("get_status", {});
    return true;
  } catch (error) {
    gatewayConnectionIssue = connectionIssueFor(error);
    return false;
  }
}

async function connectGateway(options = {}, { throwOnError = false } = {}) {
  try {
    await gatewayBridge.connect(options);
    gatewayConnectionIssue = null;
    requestStatusSnapshot();
    return true;
  } catch (error) {
    gatewayConnectionIssue = connectionIssueFor(error);
    if (throwOnError) throw error;
    return false;
  }
}

function ensureGatewayConnection(options = {}) {
  if (gatewayBridge.state().sessionId) return Promise.resolve(true);
  if (gatewayConnectPromise) return gatewayConnectPromise;
  gatewayConnectPromise = connectGateway(options);
  return gatewayConnectPromise.finally(() => {
    gatewayConnectPromise = null;
  });
}

function unavailableGatewayFrameResult() {
  const issue = connectionIssueFor({ code: "gateway_device_unavailable" });
  gatewayConnectionIssue = issue;
  return {
    frames: [],
    errors: [{ code: issue.code, message: issue.message }],
    events: [],
    operations: [],
    state: gatewayBridge.state(),
    outbound: [],
  };
}

export async function handle(request) {
  const reqType = request?.type ?? "";

  if (reqType === "gateway.session") {
    const action = request.action || "state";
    if (action === "connect") {
      await connectGateway({ deviceId: request.deviceId, identity: request.identity }, { throwOnError: true });
    }
    else if (action === "disconnect") {
      await gatewayBridge.disconnect(request.reason || "host_disconnect");
    } else if (action === "recover_sms") {
      gatewayBridge.recoverSms({ operationId: request.operationId, reason: request.reason });
    }
    else if (action === "physical_disconnect") {
      await gatewayBridge.disconnect("physical_disconnect");
    }
    else if (action !== "state") throw new Error(`unsupported gateway session action: ${action}`);
    return {
      state: gatewayBridge.state(),
      events: PROTOCOL_EVENTS.splice(0),
      ...(gatewayConnectionIssue ? { connectionIssue: gatewayConnectionIssue } : {}),
    };
  }

  if (reqType === "gateway.frame") {
    if (!gatewayBridge.state().sessionId) return unavailableGatewayFrameResult();
    const result = await gatewayBridge.receive(request.chunk ?? request.data ?? "");
    return { ...result, outbound: [] };
  }

  if (reqType === "gateway.tick") {
    const result = gatewayBridge.tick();
    return { ...result, events: PROTOCOL_EVENTS.splice(0), outbound: [] };
  }

  if (reqType === "workspace.command") {
    const cmdId = request.commandId;
    const snapshot = mergeGatewaySnapshot(await storage.cache.get(SNAPSHOT_KEY));

    if (STATUS_SLICE_ONLY) {
      if (cmdId !== "refresh") {
        return formatWorkspaceV2(snapshot, [], workspaceIssue(
          "gateway_status_slice_only",
          "当前候选仅提供真实设备状态，短信及其他副作用操作尚未启用。",
        ));
      }
      if (!await ensureGatewayConnection()) return formatWorkspaceV2(snapshot);
      requestStatusSnapshot();
      return formatWorkspaceV2(snapshot);
    }

    if (cmdId === "send_sms") {
      const values = request.values || {};
      const operationId = request.operationId;
      if (!operationId) throw new Error("send_sms requires operationId");
      if (!gatewayBridge.state().sessionId) throw new Error("gateway device is not connected");
      gatewayBridge.sendSms({ operationId, phone: values.phone, content: values.content });
      return formatWorkspaceV2(snapshot);
    }
    const bridgeConnected = Boolean(gatewayBridge.state().sessionId);
    const protocolOnline = bridgeConnected && gatewayBridge.state().protocol?.protocolOnline;
    if (cmdId === "refresh" && protocolOnline) {
      gatewayBridge.command("get_status", {});
    }
    if (cmdId === "fetch_otp" && protocolOnline) {
      gatewayBridge.command("get_latest_otp", {});
    }
    if (cmdId === "get_history" && protocolOnline) {
      gatewayBridge.command("get_sms_history", { limit: 50 });
    }
    if (cmdId === "get_history" && bridgeConnected) {
      const history = await readStoredSmsHistory({ limit: 50, cursor: request.cursor });
      latestGatewaySnapshot = { ...(latestGatewaySnapshot ?? {}), sms_history: history.items };
      return formatWorkspaceV2({ ...snapshot, sms_history: history.items });
    }
    return formatWorkspaceV2(snapshot);
  }

  if (reqType === "gateway.update_snapshot") {
    const payload = request.payload;
    const opId = request.operationId || `op_${Date.now()}`;
    await storage.cache.set(SNAPSHOT_KEY, payload, opId);
    latestGatewaySnapshot = payload && typeof payload === "object" && !Array.isArray(payload)
      ? { ...(latestGatewaySnapshot ?? {}), ...payload }
      : latestGatewaySnapshot;
    return { ok: true, timestamp: Date.now() };
  }

  await ensureGatewayConnection();
  const snapshot = mergeGatewaySnapshot(await storage.cache.get(SNAPSHOT_KEY));
  return request?.workspaceProtocol === "plugin.workspace.v1"
    ? formatStatusCard(snapshot)
    : formatWorkspaceV2(snapshot);
}

// Keep status synchronization independent of a page opening the workspace.
// A missing mapping is recorded as a recoverable issue and never creates a
// synthetic protocol session or a queued side effect.
void ensureGatewayConnection();
