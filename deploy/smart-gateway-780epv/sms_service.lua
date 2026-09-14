local M = {}
local serial_comm = require "serial_comm"
local pending_tx = {}
local active_tx = nil
local MAX_PENDING_TX = 16
local sms_frozen = false
local latest_otp_record = nil

local function next_sms_event_id()
    return serial_comm.new_event_id("sms")
end

local function watch_send(req)
    req.timer_id = sys.timerStart(function()
        if active_tx ~= req then return end
        sms_frozen = true
        req.unknown = true
        serial_comm.send_response(req.id, -408, "UNKNOWN", { reason = "modem_result_timeout" })
        -- The modem has no request IDs. Keep this slot until its late callback;
        -- do not attach that callback to a subsequent SMS.
    end, 60000)
end

local function dispatch_next()
    if active_tx or sms_frozen then return end
    local req = table.remove(pending_tx, 1)
    if not req then return end
    active_tx = req
    if not sms.send(req.to, req.text) then
        active_tx = nil
        serial_comm.send_response(req.id, -102, "SEND_FAILED")
        dispatch_next()
    else
        watch_send(req)
    end
end
function M.get_latest_otp(freshness_sec)
    if latest_otp_record and latest_otp_record.code and #latest_otp_record.code > 0 then
        if not freshness_sec or (os.time() - (latest_otp_record.time or 0) <= freshness_sec) then
            return latest_otp_record
        end
    end
    return nil
end
local function extract_auth_code(content, from)
    if not content or #content < 4 then return "" end
    local keywords = { "验证码", "校验码", "动态码", "动态密码", "授权码", "确认码", "激活码", "安全码", "检验码", "取件码", "code", "Code", "CODE", "OTP", "PIN" }
    local has_kw = false
    for _, kw in ipairs(keywords) do
        if content:find(kw, 1, true) then has_kw = true; break end
    end
    if not has_kw then return "" end
    local function is_valid(cand, pre_context, post_context)
        if not cand then return false end
        local len = #cand
        if len < 4 or len > 8 then return false end
        if from and #from > 0 and (cand == from or (len >= 7 and from:find(cand, 1, true))) then return false end
        if len == 4 and (cand:sub(1, 3) == "202" or cand:sub(1, 3) == "203") then return false end
        if pre_context then
            for _, bp in ipairs({"尾号", "卡号", "账号", "单号", "订单", "金额", "消费", "支出", "收入"}) do
                if pre_context:find(bp, 1, true) then return false end
            end
        end
        if post_context then
            for _, bu in ipairs({"元", "角", "分", "年", "月", "日", "点", "秒", "MB", "GB", "KB", "条", "位", "个", "次"}) do
                if post_context:find("^" .. bu) then return false end
            end
        end
        return true
    end
    for _, kw in ipairs(keywords) do
        local kw_pos = 1
        while true do
            local s, e = content:find(kw, kw_pos, true)
            if not s then break end
            local window = content:sub(e + 1, e + 35)
            local gap, cand, rest = window:match("^(%D-)(%d%d%d%d%d?%d?%d?%d?)(.*)")
            if cand and #gap <= 15 then
                local pre_ctx = content:sub(math.max(1, s - 10), s - 1) .. gap
                if is_valid(cand, pre_ctx, rest) then return cand end
            end
            kw_pos = e + 1
        end
    end
    for _, kw in ipairs(keywords) do
        local s, e = content:find(kw, 1, true)
        if s and s > 4 then
            local pre_window = content:sub(math.max(1, s - 40), s - 1)
            local cand = pre_window:match("(%d%d%d%d%d?%d?%d?%d?)%D*$")
            if cand then
                local pre_ctx = pre_window:sub(1, math.max(1, #pre_window - #cand))
                if is_valid(cand, pre_ctx, kw) then return cand end
            end
        end
    end
    for _, pat in ipairs({"【(%d%d%d%d%d?%d?%d?%d?)】", "%[(%d%d%d%d%d?%d?%d?%d?)%]", "%((%d%d%d%d%d?%d?%d?%d?)%)"}) do
        for cand in content:gmatch(pat) do
            if is_valid(cand) then return cand end
        end
    end
    return ""
end
function M.init()
    if sms.autoLong then sms.autoLong(true) end
    sys.subscribe("SMS_INC", function(from, content)
        log.info("sms", "SMS_INC received, content_len:", #content)
        local code = extract_auth_code(content, from)
        if #code > 0 then
            latest_otp_record = { code = code, phone = from, content = content, time = os.time() }
        end
        if led and led.event then led.event() end
        local msg_id = next_sms_event_id()
        local timestamp = os.time()
        serial_comm.publish("sms_rx", { id = msg_id, from = from, content = content, code = code, time = timestamp })
        sys.publish("NOTIFY_PUSH", "sms", from, content, code, msg_id)
        sys.publish("SMS_SAVE", from, content, code, msg_id, timestamp)
    end)
    sys.subscribe("SMS_SENT", function(result)
        log.info("sms", "SMS_SENT result:", result and "ok" or "failed")
        local req = active_tx
        active_tx = nil
        if req then
            if req.timer_id then sys.timerStop(req.timer_id) end
            sms_frozen = false
            serial_comm.send_response(req.id, result and 0 or -1, result and "SENT_OK" or "SENT_FAILED", { success = result })
            dispatch_next()
        else
            serial_comm.publish("sms_tx_report", { success = result })
        end
    end)
    sys.subscribe("SERIAL_CMD", function(cmd_packet)
        if cmd_packet.cmd == "send_sms" then
            local req_id = cmd_packet.id or ("tx_" .. os.time())
            local data = cmd_packet.data or cmd_packet.params or {}
            local to = data.to or data.phone
            local text = data.text or data.content
            if type(to) ~= "string" or #to == 0 or type(text) ~= "string" or #text == 0 then
                serial_comm.send_response(req_id, -101, "PARAM_ERR")
                return
            end
            if sms_frozen then
                serial_comm.send_response(req_id, -409, "SMS_RESULT_UNKNOWN", { reason = "previous_modem_result_pending" })
                return
            end
            if #pending_tx >= MAX_PENDING_TX then
                serial_comm.send_response(req_id, -429, "QUEUE_FULL")
                return
            end
            if active_tx or sms_frozen then
                table.insert(pending_tx, { id = req_id, to = to, text = text })
                serial_comm.send_response(req_id, 0, "QUEUED", { queue_position = #pending_tx })
            else
                active_tx = { id = req_id, to = to, text = text }
                if not sms.send(to, text) then
                    active_tx = nil
                    serial_comm.send_response(req_id, -102, "SEND_FAILED")
                    dispatch_next()
                else
                    serial_comm.send_response(req_id, 0, "QUEUED")
                    watch_send(active_tx)
                end
            end
        end
    end)
    log.info("sms", "init ok")
end
return M
