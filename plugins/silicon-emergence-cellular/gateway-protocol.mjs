const DEFAULT_MAX_FRAME_BYTES = 64 * 1024;
const PING_INTERVAL_MS = 1000;
const MIN_OFFLINE_THRESHOLD_MS = 3000;
const MAX_OFFLINE_THRESHOLD_MS = 30000;
const DEFAULT_SMS_TIMEOUT_MS = 30000;
const COMMAND_ID = /^[a-z][A-Za-z0-9_.-]{0,63}$/u;
const OPERATION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;

function protocolError(message, code = "gateway_protocol_invalid") {
  const error = new Error(message);
  error.code = code;
  return error;
}

function byteLength(value) {
  return new TextEncoder().encode(value).byteLength;
}

function assertCommandId(command) {
  if (typeof command !== "string" || !COMMAND_ID.test(command)) throw protocolError("invalid gateway command");
  return command;
}

function assertOperationId(operationId) {
  if (typeof operationId !== "string" || operationId.length > 128 || !OPERATION_ID.test(operationId)) {
    throw protocolError("invalid gateway operation id");
  }
  return operationId;
}

function assertParams(params) {
  if (!params || typeof params !== "object" || Array.isArray(params)) throw protocolError("gateway command params must be an object");
  try {
    JSON.stringify(params);
  } catch {
    throw protocolError("gateway command params must be JSON serializable");
  }
  return params;
}

export function createNdjsonDecoder({ maxFrameBytes = DEFAULT_MAX_FRAME_BYTES } = {}) {
  if (!Number.isSafeInteger(maxFrameBytes) || maxFrameBytes < 1024) throw new RangeError("maxFrameBytes must be at least 1024");
  let decoder = new TextDecoder("utf-8", { fatal: false });
  let textBuffer = "";

  function reset() {
    decoder = new TextDecoder("utf-8", { fatal: false });
    textBuffer = "";
  }

  function push(chunk) {
    if (!(typeof chunk === "string" || chunk instanceof Uint8Array || chunk instanceof ArrayBuffer)) {
      throw protocolError("gateway frame chunk must be text or bytes");
    }
    const bytes = typeof chunk === "string" ? null : chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
    textBuffer += typeof chunk === "string" ? chunk : decoder.decode(bytes, { stream: true });
    const frames = [];
    const errors = [];
    while (true) {
      const newline = textBuffer.indexOf("\n");
      if (newline < 0) break;
      const rawLine = textBuffer.slice(0, newline);
      textBuffer = textBuffer.slice(newline + 1);
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (!line.trim()) continue;
      if (byteLength(line) > maxFrameBytes) {
        errors.push(protocolError("gateway frame exceeds size limit", "gateway_frame_too_large"));
        continue;
      }
      try {
        const value = JSON.parse(line);
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("frame must be an object");
        frames.push(value);
      } catch (error) {
        errors.push(protocolError(`invalid gateway NDJSON frame: ${error.message}`));
      }
    }
    if (byteLength(textBuffer) > maxFrameBytes) throw protocolError("gateway partial frame exceeds size limit", "gateway_frame_too_large");
    return { frames, errors, bufferedBytes: byteLength(textBuffer) };
  }

  return Object.freeze({ push, reset, get bufferedBytes() { return byteLength(textBuffer); } });
}

export function createGatewaySession({
  send,
  now = Date.now,
  offlineThresholdMs = 5000,
  smsTimeoutMs = DEFAULT_SMS_TIMEOUT_MS,
  maxFrameBytes = DEFAULT_MAX_FRAME_BYTES,
  onEvent,
  onOperation,
} = {}) {
  if (typeof send !== "function") throw new TypeError("gateway session requires a send function");
  if (!Number.isSafeInteger(offlineThresholdMs)
    || offlineThresholdMs < MIN_OFFLINE_THRESHOLD_MS || offlineThresholdMs > MAX_OFFLINE_THRESHOLD_MS) {
    throw new RangeError("offlineThresholdMs must be between 3000 and 30000");
  }
  if (!Number.isSafeInteger(smsTimeoutMs) || smsTimeoutMs < 1000) throw new RangeError("smsTimeoutMs must be at least 1000");

  const decoder = createNdjsonDecoder({ maxFrameBytes });
  const pending = new Map();
  const smsQueue = [];
  const smsOperationStates = new Map();
  const unknownSmsByRequestId = new Map();
  let sessionNumber = 0;
  let sequence = 0;
  let started = false;
  let physicallyConnected = false;
  let lastActivityAt = null;
  let lastPingAt = null;
  let pingRequestId = null;
  let smsInFlight = null;
  let smsFrozen = false;

  function timestamp() {
    const value = Number(now());
    return Number.isFinite(value) ? value : Date.now();
  }

  function state() {
    const current = timestamp();
    const protocolOnline = physicallyConnected && lastActivityAt !== null && current - lastActivityAt <= offlineThresholdMs;
    return Object.freeze({
      sessionNumber,
      physicallyConnected,
      protocolOnline,
      lastActivityAt,
      lastPingAt,
      offlineThresholdMs,
      // Phone numbers and message bodies stay in the command closure. They are
      // never part of the host-facing session state or operation callbacks.
      smsInFlight: smsInFlight ? Object.freeze({
        operationId: smsInFlight.operationId,
        requestId: smsInFlight.requestId,
        state: smsInFlight.state,
        sentAt: smsInFlight.sentAt,
      }) : null,
      smsQueueLength: smsQueue.length,
      smsFrozen,
    });
  }

  function notifyEvent(event) {
    onEvent?.(event);
    return event;
  }

  function notifyOperation(operation) {
    const next = Object.freeze({
      operationId: operation.operationId,
      commandId: operation.commandId,
      state: operation.state,
      tone: operation.tone,
      text: operation.text,
      updatedAt: timestamp(),
    });
    smsOperationStates.delete(next.operationId);
    smsOperationStates.set(next.operationId, next);
    while (smsOperationStates.size > 128) smsOperationStates.delete(smsOperationStates.keys().next().value);
    onOperation?.(next);
    return next;
  }

  function nextRequestId(prefix = "req") {
    sequence += 1;
    return `${prefix}_${sessionNumber}_${sequence}`;
  }

  function write(frame, metadata = {}) {
    const line = `${JSON.stringify(frame)}\n`;
    send(line, metadata);
    return line;
  }

  function sendCommand(cmd, params = {}, {
    requestId = nextRequestId("req"),
    sideEffect = false,
    operationId,
  } = {}) {
    assertCommandId(cmd);
    assertParams(params);
    if (typeof requestId !== "string" || !requestId || requestId.length > 128) throw protocolError("invalid gateway request id");
    if (operationId !== undefined) assertOperationId(operationId);
    if (!started) throw protocolError("gateway session is not started", "gateway_session_inactive");
    const frame = { type: "cmd", id: requestId, cmd, params };
    write(frame, { requestId, cmd, operationId });
    pending.set(requestId, { cmd, sideEffect, operationId, sentAt: timestamp(), timedOut: false });
    return Object.freeze({ id: requestId, frame });
  }

  function start() {
    sessionNumber += 1;
    sequence = 0;
    started = true;
    physicallyConnected = true;
    lastActivityAt = null;
    lastPingAt = null;
    pingRequestId = null;
    decoder.reset();
    pending.clear();
    const handshake = sendCommand("ping", {}, { requestId: nextRequestId("hello") });
    pingRequestId = handshake.id;
    lastPingAt = timestamp();
    return handshake;
  }

  function setPhysicalConnected(connected) {
    physicallyConnected = Boolean(connected);
    if (!physicallyConnected) disconnect("physical_disconnect");
    return state();
  }

  function disconnect(reason = "disconnect") {
    physicallyConnected = false;
    started = false;
    lastActivityAt = null;
    pingRequestId = null;
    decoder.reset();
    for (const [requestId, request] of pending) {
      if (request.sideEffect && request.cmd !== "send_sms") {
        notifyEvent({ type: "operation", event: "UNKNOWN", data: { requestId, reason } });
      }
    }
    if (smsInFlight && smsInFlight.state !== "UNKNOWN") {
      unknownSmsByRequestId.set(smsInFlight.requestId, { ...smsInFlight, state: "UNKNOWN" });
      notifyOperation({ operationId: smsInFlight.operationId, commandId: "send_sms", state: "UNKNOWN", tone: "warning", text: "连接中断，发送结果未知" });
      smsInFlight = { ...smsInFlight, state: "UNKNOWN" };
      smsFrozen = true;
    }
    pending.clear();
    return state();
  }

  function finishSms(operation, stateName, tone, text) {
    const next = notifyOperation({ operationId: operation.operationId, commandId: "send_sms", state: stateName, tone, text });
    unknownSmsByRequestId.delete(operation.requestId);
    const isCurrent = smsInFlight?.requestId === operation.requestId;
    if (isCurrent) {
      smsInFlight = null;
      if (stateName === "UNKNOWN") smsFrozen = true;
      if (stateName === "SENT_OK" || stateName === "SENT_FAILED") smsFrozen = false;
    }
    if (!smsFrozen && smsQueue.length && started) dispatchSms(smsQueue.shift());
    return next;
  }

  function dispatchSms(operation) {
    if (!started || smsFrozen || smsInFlight) return notifyOperation({ operationId: operation.operationId, commandId: "send_sms", state: "QUEUED", tone: "warning", text: smsFrozen ? "上一笔发送结果未知，已冻结后续派发" : "已进入设备发送队列" });
    const command = sendCommand("send_sms", { phone: operation.phone, content: operation.content }, {
      sideEffect: true,
      operationId: operation.operationId,
    });
    smsInFlight = { ...operation, requestId: command.id, state: "QUEUED", sentAt: timestamp() };
    return notifyOperation({ operationId: operation.operationId, commandId: "send_sms", state: "QUEUED", tone: "warning", text: "已受理，等待设备发送" });
  }

  function queueSms({ operationId, phone, content }) {
    assertOperationId(operationId);
    const previous = smsOperationStates.get(operationId);
    if (previous) return notifyOperation(previous);
    if (typeof phone !== "string" || !phone.trim() || phone.length > 32) throw protocolError("invalid SMS phone");
    if (typeof content !== "string" || !content.trim() || content.length > 2000) throw protocolError("invalid SMS content");
    const operation = { operationId, phone, content };
    if (smsInFlight || smsFrozen || !started) {
      smsQueue.push(operation);
      return notifyOperation({ operationId, commandId: "send_sms", state: "QUEUED", tone: "warning", text: smsFrozen ? "上一笔发送结果未知，后续短信暂缓派发" : "已进入 NAS 队列，等待设备空闲" });
    }
    return dispatchSms(operation);
  }

  function acknowledgeSmsRecovery({ operationId, reason } = {}) {
    if (!smsFrozen) return state();
    if (!operationId || !reason) throw protocolError("SMS recovery requires the unknown operation and an explicit reason");
    if (smsInFlight?.operationId !== operationId
      && !smsQueue.some((item) => item.operationId === operationId)
      && ![...unknownSmsByRequestId.values()].some((item) => item.operationId === operationId)) {
      throw protocolError("unknown SMS operation cannot be recovered");
    }
    smsFrozen = false;
    if (smsInFlight?.operationId === operationId && smsInFlight.state === "UNKNOWN") smsInFlight = null;
    if (!smsInFlight && smsQueue.length && started) dispatchSms(smsQueue.shift());
    return state();
  }

  function handleResponse(frame) {
    const request = pending.get(frame.id);
    const unknownSms = unknownSmsByRequestId.get(frame.id) || null;
    if (!request && !unknownSms) return null;
    const isSms = Boolean(unknownSms) || request?.cmd === "send_sms" && (
      smsInFlight?.requestId === frame.id || unknownSmsByRequestId.has(frame.id)
    );
    const smsOperation = unknownSms || (isSms ? smsInFlight : null);
    const message = typeof frame.msg === "string" ? frame.msg : "";
    const code = Number.isInteger(frame.code) ? frame.code : -1;
    if (request?.cmd === "ping") {
      pending.delete(frame.id);
      if (pingRequestId === frame.id) pingRequestId = null;
      const event = notifyEvent({ type: "protocol", event: "pong", data: { requestId: frame.id, code, msg: message } });
      if (code === 0 && !smsInFlight && !smsFrozen && smsQueue.length && started) dispatchSms(smsQueue.shift());
      return event;
    }
    if (isSms && smsOperation) {
      if (message === "QUEUED" && code === 0) {
        return notifyOperation({ operationId: smsOperation.operationId, commandId: "send_sms", state: "QUEUED", tone: "warning", text: "设备已受理，等待发送回执" });
      }
      if (message === "SENT_OK" && code === 0) {
        pending.delete(frame.id);
        return finishSms(smsOperation, "SENT_OK", "success", "模组发送成功，等待接收方业务回执");
      }
      if (message === "SMS_RESULT_UNKNOWN" || code === -409) {
        pending.delete(frame.id);
        // The board rejected this new request because an older modem result
        // is still unresolved. Keep the older active slot and freeze the
        // queue; this response is a terminal rejection for the new request,
        // not proof that the modem completed it.
        smsFrozen = true;
        return notifyOperation({
          operationId: smsOperation.operationId,
          commandId: "send_sms",
          state: "SENT_FAILED",
          tone: "error",
          text: "设备仍在等待上一笔短信结果，当前请求未派发",
        });
      }
      if (message === "SENT_FAILED" || message === "UNKNOWN" || code === -408 || code < 0) {
        pending.delete(frame.id);
        if (message === "UNKNOWN" || code === -408) {
          unknownSmsByRequestId.set(frame.id, { ...smsOperation, state: "UNKNOWN" });
          const result = notifyOperation({ operationId: smsOperation.operationId, commandId: "send_sms", state: "UNKNOWN", tone: "warning", text: "设备未给出明确发送结果" });
          if (smsInFlight?.requestId === frame.id) smsInFlight = { ...smsInFlight, state: "UNKNOWN" };
          smsFrozen = true;
          return result;
        }
        return finishSms(smsOperation, "SENT_FAILED", "error", message || "模组发送失败");
      }
      return notifyOperation({ operationId: smsOperation.operationId, commandId: "send_sms", state: "ACCEPTED", tone: "warning", text: "设备已接受请求，等待明确发送结果" });
    }
    pending.delete(frame.id);
    return notifyEvent({ type: "response", event: "response", data: { ...frame, code } });
  }

  function receive(chunk) {
    const decoded = decoder.push(chunk);
    const events = [];
    const operations = [];
    if (!started) {
      // A modem callback can arrive after a physical disconnect. Only a
      // response tied to the preserved UNKNOWN SMS slot may close that slot;
      // disconnected sessions must not become online from arbitrary input.
      for (const frame of decoded.frames) {
        if (frame.type !== "res" || typeof frame.id !== "string" || !unknownSmsByRequestId.has(frame.id)) continue;
        const result = handleResponse(frame);
        if (result) (result.commandId ? operations : events).push(result);
      }
      return { frames: decoded.frames, errors: decoded.errors, events, operations, state: state() };
    }
    physicallyConnected = true;
    for (const frame of decoded.frames) {
      lastActivityAt = timestamp();
      if (frame.type === "res" && typeof frame.id === "string") {
        const result = handleResponse(frame);
        if (result) (result.commandId ? operations : events).push(result);
        continue;
      }
      if (frame.type === "event" && typeof frame.event === "string") {
        events.push(notifyEvent({ type: "event", event: frame.event, data: frame.data, ts: frame.ts }));
      } else {
        decoded.errors.push(protocolError("unsupported gateway frame type"));
      }
    }
    return { frames: decoded.frames, errors: decoded.errors, events, operations, state: state() };
  }

  function tick() {
    const current = timestamp();
    const operations = [];
    if (started && physicallyConnected && pingRequestId && lastPingAt !== null && current - lastPingAt >= PING_INTERVAL_MS) {
      pending.delete(pingRequestId);
      pingRequestId = null;
    }
    if (started && physicallyConnected && !pingRequestId && (lastPingAt === null || current - lastPingAt >= PING_INTERVAL_MS)) {
      const ping = sendCommand("ping", {}, { requestId: nextRequestId("ping") });
      pingRequestId = ping.id;
      lastPingAt = current;
    }
    if (smsInFlight && smsInFlight.state !== "UNKNOWN" && current - smsInFlight.sentAt >= smsTimeoutMs) {
      const timedOutRequest = pending.get(smsInFlight.requestId);
      if (timedOutRequest) timedOutRequest.timedOut = true;
      unknownSmsByRequestId.set(smsInFlight.requestId, { ...smsInFlight, state: "UNKNOWN" });
      const result = notifyOperation({ operationId: smsInFlight.operationId, commandId: "send_sms", state: "UNKNOWN", tone: "warning", text: "等待发送结果超时，未自动重发" });
      operations.push(result);
      smsInFlight = { ...smsInFlight, state: "UNKNOWN" };
      smsFrozen = true;
    }
    return { operations, state: state() };
  }

  return Object.freeze({
    start,
    disconnect,
    setPhysicalConnected,
    sendCommand,
    queueSms,
    acknowledgeSmsRecovery,
    receive,
    tick,
    state,
    get pendingCount() { return pending.size; },
  });
}

export const GATEWAY_NDJSON_PROTOCOL = Object.freeze({
  request: Object.freeze({ type: "cmd", id: "request-id", cmd: "ping", params: {} }),
  response: Object.freeze({ type: "res", id: "request-id", code: 0, msg: "pong", data: {} }),
  event: Object.freeze({ type: "event", event: "sms_rx", data: {}, ts: 0 }),
  pingIntervalMs: PING_INTERVAL_MS,
  offlineThresholdMs: Object.freeze({ min: MIN_OFFLINE_THRESHOLD_MS, max: MAX_OFFLINE_THRESHOLD_MS }),
});
