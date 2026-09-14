import { createGatewaySession } from "./gateway-protocol.mjs";

const MAX_MESSAGE_ID = 256;
const MAX_RECORD_TEXT = 2000;
const PERSISTED_OPERATION_TYPE = "gateway.operation";
const PENDING_OPERATION_STATES = new Set(["QUEUED", "ACCEPTED", "SENDING", "SYNCING", "RETRYABLE"]);

function bridgeError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function text(value, fallback = null, max = MAX_RECORD_TEXT) {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  return trimmed && trimmed.length <= max ? trimmed : fallback;
}

function messageId(value) {
  const id = text(value, null, MAX_MESSAGE_ID);
  return id;
}

function safeError(error, fallback = "gateway_bridge_error") {
  return {
    code: typeof error?.code === "string" ? error.code.slice(0, 128) : fallback,
    message: typeof error?.message === "string" ? error.message.slice(0, 256) : fallback,
  };
}

function safeOperation(operation) {
  if (!operation || typeof operation !== "object") return null;
  return {
    operationId: operation.operationId,
    commandId: operation.commandId,
    state: operation.state,
    tone: operation.tone,
    text: operation.text,
    updatedAt: operation.updatedAt,
  };
}

function safeFrame(frame) {
  if (!frame || typeof frame !== "object" || Array.isArray(frame)) return null;
  if (frame.type === "event") {
    const id = messageId(frame.data?.id ?? frame.data?.msg_id);
    return {
      type: "event",
      event: typeof frame.event === "string" ? frame.event : "unknown",
      ...(frame.ts === undefined ? {} : { ts: frame.ts }),
      ...(id ? { data: { id } } : {}),
    };
  }
  if (frame.type === "res") {
    return {
      type: "res",
      id: typeof frame.id === "string" ? frame.id : "unknown",
      code: Number.isInteger(frame.code) ? frame.code : -1,
      msg: typeof frame.msg === "string" ? frame.msg.slice(0, 128) : "",
    };
  }
  return { type: typeof frame.type === "string" ? frame.type : "unknown" };
}

function safeProtocolEvent(event) {
  if (!event || typeof event !== "object") return null;
  if (event.type === "event") {
    const id = messageId(event.data?.id ?? event.data?.msg_id);
    return {
      type: "gateway.event",
      event: typeof event.event === "string" ? event.event : "unknown",
      ...(id ? { messageId: id } : {}),
      ...(event.ts === undefined ? {} : { ts: event.ts }),
    };
  }
  if (event.type === "protocol") {
    return {
      type: "gateway.protocol",
      event: typeof event.event === "string" ? event.event : "unknown",
      data: {
        requestId: text(event.data?.requestId, "unknown", 128),
        code: Number.isInteger(event.data?.code) ? event.data.code : -1,
        msg: text(event.data?.msg, "", 128),
      },
    };
  }
  if (event.type === "response") {
    return {
      type: "gateway.response",
      id: text(event.data?.id, "unknown", 128),
      code: Number.isInteger(event.data?.code) ? event.data.code : -1,
      msg: text(event.data?.msg, "", 128),
    };
  }
  return null;
}

function unsubscribe(subscription) {
  try {
    if (typeof subscription === "function") subscription();
    else subscription?.unsubscribe?.();
  } catch {
    // Listener cleanup is best effort; the device session remains the owner.
  }
}

function normalizeDeviceStatus(value) {
  const raw = typeof value === "string" ? value : value?.state;
  return typeof raw === "string" && raw ? raw : "UNKNOWN";
}

function persistedOperation(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const safe = safeOperation(value);
  if (!safe?.operationId || !safe.commandId || !safe.state || !safe.tone || !safe.text) return null;
  if (!Number.isSafeInteger(safe.updatedAt) || safe.updatedAt < 0) return null;
  return safe;
}

function normalizeStatusSnapshot(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = { ...value };
  const hardware = { ...(source.hardware && typeof source.hardware === "object" && !Array.isArray(source.hardware) ? source.hardware : {}) };
  const network = { ...(source.cellular_network && typeof source.cellular_network === "object" && !Array.isArray(source.cellular_network) ? source.cellular_network : {}) };
  const system = { ...(source.system_status && typeof source.system_status === "object" && !Array.isArray(source.system_status) ? source.system_status : {}) };
  const temperature = source.temperature_celsius ?? source.temperature ?? source.temp;
  const voltage = source.voltage_vbat ?? source.voltage ?? source.vbat;
  if (source.temperature_valid === false || source.adc_valid?.temperature === false) hardware.temperature_celsius = null;
  else if (temperature !== undefined) hardware.temperature_celsius = temperature;
  if (source.voltage_valid === false || source.adc_valid?.voltage === false) hardware.voltage_vbat = null;
  else if (voltage !== undefined) hardware.voltage_vbat = voltage;
  if (source.model !== undefined || source.bsp !== undefined) hardware.model = source.model ?? source.bsp;
  if (source.csq_signal !== undefined || source.csq !== undefined) network.csq_signal = source.csq_signal ?? source.csq;
  if (source.rsrp_dbm !== undefined || source.rsrp !== undefined) network.rsrp_dbm = source.rsrp_dbm ?? source.rsrp;
  if (source.network_ready !== undefined || source.net_ready !== undefined || source.registered !== undefined) {
    network.network_ready = source.network_ready ?? source.net_ready ?? source.registered;
  }
  if (source.rndis !== undefined || source.rndis_internet_sharing !== undefined) {
    system.rndis_internet_sharing = source.rndis_internet_sharing ?? source.rndis;
  }
  if (source.cellular_data_mode !== undefined || source.cellular_data_enabled !== undefined) {
    system.cellular_data_mode = source.cellular_data_mode ?? source.cellular_data_enabled;
  }
  if (source.continuous_uptime !== undefined || source.uptime_seconds !== undefined) {
    system.continuous_uptime = source.continuous_uptime ?? source.uptime_seconds;
  }
  return {
    ...source,
    hardware,
    cellular_network: network,
    system_status: system,
    ...(source.number !== undefined || source.phone !== undefined ? { phone: source.number ?? source.phone } : {}),
  };
}

/**
 * Connect the NDJSON state machine to the host-owned devices and records SDK.
 *
 * The bridge intentionally has no notification transport. It persists a
 * received message before sending the board-only notify_ack claim, then
 * exposes a redacted claim result for a separately authorized host delivery
 * step. Message bodies remain only in the records request and device write;
 * they never enter bridge state, operation callbacks, or returned frames.
 */
export function createGatewaySdkBridge({
  devices,
  records,
  now = Date.now,
  restoreOperationsOnConnect = true,
  onEvent,
  onOperation,
  onSnapshot,
} = {}) {
  if (!devices || typeof devices.list !== "function" || typeof devices.open !== "function") {
    throw new TypeError("gateway bridge requires the devices SDK");
  }
  const recordsAvailable = Boolean(records
    && typeof records.append === "function"
    && typeof records.list === "function");

  let connection = null;
  let protocol = null;
  let deviceStatus = "DISCONNECTED";
  let dataSubscription = null;
  let statusSubscription = null;
  let dataChain = Promise.resolve();
  const claimRequests = new Map();
  const controlRequests = new Map();
  const claimedMessageIds = new Set();
  const recordedMessageIds = new Set();
  const recordedCallIds = new Set();
  const operations = new Map();
  let operationWriteChain = Promise.resolve();
  let operationSequence = 0;
  let operationsRestored = false;
  let lastSelection = null;
  let reconnectNeeded = false;
  let reconnectAt = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;
  let reconnectPromise = null;

  function currentTime() {
    const value = Number(now());
    return Number.isFinite(value) ? value : Date.now();
  }

  function emit(event) {
    if (!event || typeof event !== "object") return null;
    onEvent?.(Object.freeze({ ...event }));
    return event;
  }

  function rememberOperation(safe) {
    if (!safe?.operationId) return safe;
    operations.delete(safe.operationId);
    operations.set(safe.operationId, safe);
    while (operations.size > 32) operations.delete(operations.keys().next().value);
    return safe;
  }

  function persistOperation(operation) {
    if (!recordsAvailable) return Promise.resolve();
    const recordId = `gateway-operation:${operation.operationId}:${++operationSequence}`;
    operationWriteChain = operationWriteChain.then(async () => {
      try {
        await records.append({
          type: PERSISTED_OPERATION_TYPE,
          source: "gateway",
          deviceId: connection?.deviceId ?? null,
          recordId,
          operationId: recordId,
          payload: { ...operation },
        });
      } catch (error) {
        emit({ type: "gateway.status", state: "OPERATION_RECORD_FAILED", error: safeError(error, "plugin_records_unavailable") });
      }
    });
    return operationWriteChain;
  }

  function recordOperation(operation, { persist = true } = {}) {
    const safe = safeOperation(operation);
    if (!safe?.operationId) return safe;
    rememberOperation(safe);
    onOperation?.(Object.freeze({ ...safe }));
    if (persist) void persistOperation(safe);
    return safe;
  }

  function state() {
    return Object.freeze({
      deviceId: connection?.deviceId ?? null,
      sessionId: connection?.sessionId ?? null,
      identity: connection?.identity?.fingerprint ? { fingerprint: connection.identity.fingerprint } : null,
      deviceStatus,
      protocol: protocol?.state() ?? null,
      pendingClaims: claimRequests.size,
      pendingControls: controlRequests.size,
      operations: Object.freeze([...operations.values()].map((operation) => Object.freeze({ ...operation }))),
    });
  }

  async function restoreOperations() {
    if (operationsRestored) return;
    if (!recordsAvailable) {
      operationsRestored = true;
      return;
    }
    try {
      const listed = await records.list({ limit: 200 });
      const latest = new Map();
      for (const item of Array.isArray(listed?.items) ? listed.items : []) {
        if (item?.type !== PERSISTED_OPERATION_TYPE) continue;
        const candidate = persistedOperation(item.payload);
        if (!candidate) continue;
        const previous = latest.get(candidate.operationId);
        if (!previous || candidate.updatedAt >= previous.updatedAt) latest.set(candidate.operationId, candidate);
      }
      for (const candidate of latest.values()) {
        if (PENDING_OPERATION_STATES.has(candidate.state)) {
          recordOperation({
            ...candidate,
            state: "UNKNOWN",
            tone: "warning",
            text: "插件重启后上一笔副作用结果未知，未自动重放",
            updatedAt: currentTime(),
          });
        } else {
          recordOperation(candidate, { persist: false });
        }
      }
      operationsRestored = true;
    } catch (error) {
      operationsRestored = true;
      emit({ type: "gateway.status", state: "OPERATION_RESTORE_FAILED", error: safeError(error, "plugin_records_unavailable") });
    }
  }

  function writeProtocol(line, metadata = {}) {
    const current = connection;
    if (!current) throw bridgeError("gateway_device_unavailable", "gateway device is not connected");
    const operationId = metadata.operationId
      ? `gateway-write:${metadata.operationId}`
      : `gateway-write:${metadata.requestId ?? "control"}`;
    const bytes = new TextEncoder().encode(line);
    void Promise.resolve(current.session.write(bytes, { operationId })).catch((error) => {
      if (connection?.session !== current.session) return;
      deviceStatus = "ERROR";
      protocol?.disconnect("device_write_failed");
      emit({ type: "gateway.status", state: "ERROR", error: safeError(error, "hardware_device_write_failed") });
    });
  }

  function createProtocol() {
    return createGatewaySession({
      send: writeProtocol,
      now,
      onOperation: (operation) => { recordOperation(operation); },
      onEvent: (event) => {
        const safe = safeProtocolEvent(event);
        if (safe) emit(safe);
      },
    });
  }

  async function persistIncomingSms(data, current) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      emit({ type: "gateway.sms", state: "REJECTED", reason: "event_payload_invalid" });
      return;
    }
    const id = messageId(data.id ?? data.msg_id);
    const payload = {
      id,
      identity: id ? "stable" : "unknown",
      from: text(data.from ?? data.sender ?? data.phone, null, 128),
      content: text(data.content, null, MAX_RECORD_TEXT),
      code: text(data.code ?? data.otp, null, 32),
      time: data.time ?? data.timestamp ?? null,
    };
    if (id && (claimedMessageIds.has(id) || recordedMessageIds.has(id))) return;
    const request = {
      type: "sms.received",
      source: "gateway",
      deviceId: current.deviceId,
      payload,
      ...(id ? { recordId: id, operationId: `sms-record:${id}` } : {}),
    };
    let stored;
    try {
      stored = await records.append(request);
    } catch (error) {
      emit({ type: "gateway.sms", state: "RECORD_FAILED", ...(id ? { messageId: id } : {}), error: safeError(error, "plugin_records_unavailable") });
      return;
    }
    emit({
      type: "gateway.sms",
      state: "RECORDED",
      ...(id ? { messageId: id } : {}),
      recordId: text(stored?.recordId, null, 256),
    });
    onSnapshot?.(Object.freeze({ latest_sms: Object.freeze({
      ...payload,
      sender: payload.from,
      phone: payload.from,
      otp: payload.code,
    }) }));
    if (!id || claimedMessageIds.has(id) || recordedMessageIds.has(id)) return;
    if ([...claimRequests.values()].some((claim) => claim.messageId === id)) return;
    const requestId = `notify_ack_${id}`;
    try {
      const command = protocol.sendCommand("notify_ack", { id, status: "handled" }, {
        requestId,
        sideEffect: true,
        operationId: `notify-claim:${id}`,
      });
      claimRequests.set(command.id, { messageId: id });
      recordedMessageIds.add(id);
      emit({ type: "gateway.notification", state: "CLAIM_PENDING", messageId: id });
    } catch (error) {
      emit({ type: "gateway.notification", state: "CLAIM_FAILED", messageId: id, error: safeError(error, "notify_claim_failed") });
    }
  }

  async function persistIncomingCall(data, current) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      emit({ type: "gateway.call", state: "REJECTED", reason: "event_payload_invalid" });
      return;
    }
    const id = messageId(data.id ?? data.msg_id);
    const payload = {
      id,
      sender: text(data.from ?? data.sender ?? data.phone, null, 128),
      action: text(data.action ?? data.status, "unknown", 128),
      time: data.time ?? data.timestamp ?? null,
    };
    if (id && recordedCallIds.has(id)) return;
    try {
      const stored = await records.append({
        type: "call.received",
        source: "gateway",
        deviceId: current.deviceId,
        payload,
        ...(id ? { recordId: id, operationId: `call-record:${id}` } : {}),
      });
      if (id) recordedCallIds.add(id);
      emit({
        type: "gateway.call",
        state: "RECORDED",
        ...(id ? { messageId: id } : {}),
        recordId: text(stored?.recordId, null, 256),
      });
      onSnapshot?.(Object.freeze({ call_records: Object.freeze([Object.freeze({ ...payload })]) }));
    } catch (error) {
      emit({ type: "gateway.call", state: "RECORD_FAILED", ...(id ? { messageId: id } : {}), error: safeError(error, "plugin_records_unavailable") });
    }
  }

  async function persistHistoricalSms(data, current) {
    const source = data?.payload && typeof data.payload === "object" ? data.payload : data;
    if (!source || typeof source !== "object" || Array.isArray(source)) return;
    const id = messageId(source.id ?? source.msg_id ?? data.recordId);
    const payload = {
      id,
      identity: id ? "stable" : "unknown",
      from: text(source.from ?? source.sender ?? source.phone, null, 128),
      content: text(source.content, null, MAX_RECORD_TEXT),
      code: text(source.code ?? source.otp, null, 32),
      time: source.time ?? source.timestamp ?? data.occurredAt ?? null,
    };
    try {
      await records.append({
        type: "sms.history",
        source: "gateway-history",
        deviceId: current.deviceId,
        payload,
        ...(id ? { recordId: id, operationId: `sms-history:${id}` } : {}),
      });
    } catch (error) {
      emit({ type: "gateway.sms", state: "HISTORY_RECORD_FAILED", ...(id ? { messageId: id } : {}), error: safeError(error, "plugin_records_unavailable") });
    }
  }

  function resolveClaim(frame) {
    const claim = claimRequests.get(frame?.id);
    if (!claim) return;
    claimRequests.delete(frame.id);
    const code = Number.isInteger(frame.code) ? frame.code : -1;
    const msg = typeof frame.msg === "string" ? frame.msg : "";
    const accepted = code === 0 && msg === "NOTIFY_CLAIMED";
    const expired = code === -409 || msg === "NOTIFY_CLAIM_EXPIRED";
    const stateName = accepted ? "CLAIMED" : expired ? "EXPIRED" : "UNKNOWN";
    if (accepted) claimedMessageIds.add(claim.messageId);
    emit({
      type: "gateway.notification",
      state: stateName,
      messageId: claim.messageId,
      code,
      ...(msg ? { message: msg.slice(0, 128) } : {}),
    });
  }

  function recordControlOperation(control, stateName, tone, textValue) {
    if (!control?.operationId) return null;
    return recordOperation({
      operationId: control.operationId,
      commandId: control.commandId,
      state: stateName,
      tone,
      text: textValue,
      updatedAt: currentTime(),
    });
  }

  async function resolveControlResponse(frame) {
    const control = controlRequests.get(frame?.id);
    if (!control) return;
    controlRequests.delete(frame.id);
    const code = Number.isInteger(frame.code) ? frame.code : -1;
    if (code !== 0) {
      emit({ type: "gateway.control", state: "FAILED", commandId: control.commandId, code });
      recordControlOperation(control, control.sideEffect ? "UNKNOWN" : "RETRYABLE", "error", control.sideEffect
        ? "设备副作用结果未知，未自动重放"
        : "设备查询失败，可再次刷新");
      return;
    }
    const data = frame.data;
    if (control.commandId === "get_status" && data && typeof data === "object" && !Array.isArray(data)) {
      onSnapshot?.(Object.freeze(normalizeStatusSnapshot(data)));
      emit({ type: "gateway.control", state: "SNAPSHOT", commandId: control.commandId });
      recordControlOperation(control, "SYNCED", "success", "设备状态已刷新");
      return;
    }
    if (control.commandId === "get_latest_otp" && data && typeof data === "object" && !Array.isArray(data)) {
      onSnapshot?.(Object.freeze({ latest_sms: { ...data } }));
      emit({ type: "gateway.control", state: "SNAPSHOT", commandId: control.commandId });
      recordControlOperation(control, "SYNCED", "success", "最新验证码已更新");
      return;
    }
    if (control.commandId === "get_sms_history" && Array.isArray(data?.items)) {
      const current = connection ?? { deviceId: null };
      for (const item of data.items) await persistHistoricalSms(item, current);
      onSnapshot?.(Object.freeze({ sms_history: Object.freeze(data.items.map((item) => ({ ...(item?.payload ?? item) }))) }));
      emit({ type: "gateway.control", state: "HISTORY", commandId: control.commandId, count: data.items.length });
      recordControlOperation(control, "SYNCED", "success", "短信历史已更新");
      return;
    }
    if (control.commandId === "clear_history" || control.commandId === "clear_sms_history") {
      onSnapshot?.(Object.freeze({ sms_history: Object.freeze([]) }));
      emit({ type: "gateway.control", state: "CLEARED", commandId: control.commandId });
      recordControlOperation(control, "SYNCED", "success", "板端短信历史已清空");
      return;
    }
    if (data && typeof data === "object" && !Array.isArray(data)) {
      const normalized = normalizeStatusSnapshot(data);
      const system = { ...normalized.system_status, ...data };
      onSnapshot?.(Object.freeze({ ...normalized, system_status: system }));
    }
    emit({ type: "gateway.control", state: "ACCEPTED", commandId: control.commandId });
    recordControlOperation(control, control.commandId === "reboot" ? "ACCEPTED" : "SYNCING", "warning",
      control.commandId === "reboot" ? "重启指令已受理，等待设备恢复" : "设备已受理，等待状态回读");
  }

  async function processData(bytes, sourceEvent) {
    if (!protocol) return { frames: [], errors: [], events: [], operations: [], state: state() };
    const result = protocol.receive(bytes);
    const protocolEvents = result.events ?? [];
    for (const event of protocolEvents) {
      if (event?.type === "event" && event.event === "sms_rx") {
        await persistIncomingSms(event.data, connection ?? { deviceId: sourceEvent?.deviceId ?? null });
      } else if (event?.type === "event" && event.event === "call_rx") {
        await persistIncomingCall(event.data, connection ?? { deviceId: sourceEvent?.deviceId ?? null });
      } else if (event?.type === "event" && (event.event === "gateway_ready" || event.event === "status" || event.event === "status_update")) {
        const snapshot = normalizeStatusSnapshot(event.data);
        if (snapshot) onSnapshot?.(Object.freeze(snapshot));
      } else if (event?.type === "response" && event.event === "response") {
        await resolveControlResponse(event.data);
        resolveClaim(event.data);
      }
    }
    return {
      ...result,
      frames: result.frames.map(safeFrame).filter(Boolean),
      events: protocolEvents.map(safeProtocolEvent).filter(Boolean),
      operations: result.operations.map(safeOperation).filter(Boolean),
      state: state(),
    };
  }

  function clearReconnectTimer() {
    if (reconnectTimer === null) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function scheduleReconnect(delayMs = 0) {
    if (!reconnectNeeded || reconnectTimer !== null) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      void attemptReconnect();
    }, Math.max(0, delayMs));
  }

  async function closeCurrent(reason = "host_disconnect", { preserveProtocol = true } = {}) {
    const current = connection;
    if (!current) return state();
    protocol?.disconnect(reason);
    controlRequests.clear();
    for (const claim of claimRequests.values()) {
      emit({ type: "gateway.notification", state: "UNKNOWN", messageId: claim.messageId, reason });
    }
    claimRequests.clear();
    unsubscribe(dataSubscription);
    unsubscribe(statusSubscription);
    dataSubscription = null;
    statusSubscription = null;
    connection = null;
    if (!preserveProtocol) protocol = null;
    deviceStatus = "DISCONNECTED";
    try { await current.session.close(reason); } catch (error) {
      emit({ type: "gateway.status", state: "ERROR", error: safeError(error, "hardware_device_close_failed") });
    }
    emit({ type: "gateway.status", state: "DISCONNECTED" });
    return state();
  }

  async function attachConnection(selected) {
    const session = await devices.open(selected.deviceId, { identity: selected.identity });
    if (session.identity?.fingerprint && selected.identity?.fingerprint
      && session.identity.fingerprint !== selected.identity.fingerprint) {
      try { await session.close("identity_mismatch"); } catch { /* no session is retained */ }
      throw bridgeError("gateway_device_identity_mismatch", "opened gateway identity does not match the selected mapping");
    }
    connection = {
      deviceId: session.deviceId,
      sessionId: session.sessionId,
      identity: session.identity ?? selected.identity,
      session,
    };
    deviceStatus = "OPEN";
    if (!protocol) protocol = createProtocol();
    dataSubscription = session.onData((bytes, event) => {
      if (connection?.session !== session) return;
      dataChain = dataChain
        .then(() => processData(bytes, event))
        .catch((error) => emit({ type: "gateway.status", state: "ERROR", error: safeError(error) }));
    });
    statusSubscription = session.onStatus((event) => {
      if (connection?.session !== session) return;
      const status = normalizeDeviceStatus(event);
      deviceStatus = status;
      emit({ type: "gateway.status", state: status });
      if (["DISCONNECTED", "ERROR", "REVOKED"].includes(status)) {
        reconnectNeeded = true;
        reconnectAt = currentTime();
        emit({ type: "gateway.status", state: "RECOVERING", reason: `device_${status.toLowerCase()}` });
        scheduleReconnect(0);
      }
    });
    protocol.start();
    return state();
  }

  async function attemptReconnect() {
    if (!reconnectNeeded || reconnectPromise || !lastSelection) return state();
    const wait = reconnectAt === null ? 0 : reconnectAt - currentTime();
    if (wait > 0) {
      scheduleReconnect(wait);
      return state();
    }
    reconnectPromise = (async () => {
      await closeCurrent("auto_reconnect", { preserveProtocol: true });
      try {
        const listed = await devices.list();
        const available = Array.isArray(listed?.devices) ? listed.devices : [];
        const selected = available.find((candidate) => candidate.deviceId === lastSelection.deviceId);
        if (!selected) throw bridgeError("gateway_device_not_found", "mapped gateway device is not available");
        if (lastSelection.identity?.fingerprint && selected.identity?.fingerprint !== lastSelection.identity.fingerprint) {
          throw bridgeError("gateway_device_identity_mismatch", "reconnected gateway identity does not match the previous mapping");
        }
        if (!reconnectNeeded) return;
        await attachConnection(selected);
        reconnectNeeded = false;
        reconnectAt = null;
        reconnectAttempts = 0;
        emit({ type: "gateway.status", state: "RECOVERED", deviceId: selected.deviceId });
      } catch (error) {
        reconnectAttempts += 1;
        const delay = Math.min(1000 * (2 ** Math.min(reconnectAttempts, 4)), 10000);
        reconnectAt = currentTime() + delay;
        emit({ type: "gateway.status", state: "RECONNECT_WAITING", error: safeError(error, "gateway_reconnect_failed"), retryInMs: delay });
        scheduleReconnect(delay);
      }
    })();
    try { await reconnectPromise; } finally { reconnectPromise = null; }
    return state();
  }

  async function connect({ deviceId, identity } = {}) {
    if (reconnectPromise) await reconnectPromise;
    if (connection) {
      if (!deviceId || deviceId === connection.deviceId) return state();
      throw bridgeError("gateway_device_busy", "another gateway device is already connected");
    }
    clearReconnectTimer();
    reconnectNeeded = false;
    reconnectAt = null;
    reconnectAttempts = 0;
    const listed = await devices.list();
    const available = Array.isArray(listed?.devices) ? listed.devices : [];
    const selected = deviceId
      ? available.find((candidate) => candidate.deviceId === deviceId)
      : available.length === 1 ? available[0] : null;
    if (!selected) {
      throw bridgeError(deviceId ? "gateway_device_not_found" : "gateway_device_selection_required", deviceId ? "selected gateway device is unavailable" : "select one mapped gateway device");
    }
    if (identity?.fingerprint && identity.fingerprint !== selected.identity?.fingerprint) {
      throw bridgeError("gateway_device_identity_mismatch", "gateway device identity does not match the selected mapping");
    }
    if (protocol && lastSelection?.deviceId && lastSelection.deviceId !== selected.deviceId) protocol = null;
    lastSelection = { deviceId: selected.deviceId, identity: selected.identity };
    if (restoreOperationsOnConnect) await restoreOperations();
    return attachConnection(selected);
  }

  async function disconnect(reason = "host_disconnect") {
    reconnectNeeded = false;
    reconnectAt = null;
    clearReconnectTimer();
    if (reconnectPromise) await reconnectPromise;
    return closeCurrent(reason);
  }

  function receive(bytes) {
    dataChain = dataChain.then(() => processData(bytes, null));
    return dataChain;
  }

  function tick() {
    if (reconnectNeeded && !reconnectPromise && (!reconnectAt || reconnectAt <= currentTime())) void attemptReconnect();
    if (!protocol) return { operations: [], state: state() };
    const result = protocol.tick();
    return {
      ...result,
      operations: result.operations.map(safeOperation).filter(Boolean),
      state: state(),
    };
  }

  function command(commandId, params = {}, options = {}) {
    if (!protocol) throw bridgeError("gateway_device_unavailable", "gateway device is not connected");
    const command = protocol.sendCommand(commandId, params, options);
    controlRequests.set(command.id, {
      commandId,
      sideEffect: options.sideEffect === true,
      ...(options.operationId ? { operationId: options.operationId } : {}),
    });
    if (options.operationId) {
      recordOperation({
        operationId: options.operationId,
        commandId,
        state: "QUEUED",
        tone: "warning",
        text: "已发送设备指令，等待明确回执",
        updatedAt: currentTime(),
      });
    }
    return command;
  }

  function sendSms(input) {
    if (!protocol) throw bridgeError("gateway_device_unavailable", "gateway device is not connected");
    const operation = protocol.queueSms(input);
    return recordOperation(operation);
  }

  function recoverSms(input) {
    if (!protocol) throw bridgeError("gateway_device_unavailable", "gateway device is not connected");
    return protocol.acknowledgeSmsRecovery(input);
  }

  return Object.freeze({
    connect,
    disconnect,
    receive,
    tick,
    command,
    sendSms,
    recoverSms,
    listRecords: (input = {}) => {
      if (!recordsAvailable) throw bridgeError("plugin_records_unavailable", "plugin records permission is not available");
      return records.list(input);
    },
    clearRecords: (input = {}) => {
      if (!recordsAvailable || typeof records.clear !== "function") {
        throw bridgeError("plugin_records_unavailable", "plugin records permission is not available");
      }
      return records.clear(input);
    },
    state,
  });
}
