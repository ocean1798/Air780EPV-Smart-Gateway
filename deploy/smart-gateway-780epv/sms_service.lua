local M = {}
local model = require "model"
local serial_comm = require "serial_comm"
local pending_tx = {}
local active_tx = nil
local MAX_PENDING_TX = 16
local sms_frozen = false
local latest_otp_record = nil
local pending_store_sms = {}

local function next_sms_event_id()
    return serial_comm.new_event_id("sms")
end

local function execute_sms_send(to, text)
    local is_short_code = (#to <= 8 and to:match("^%d+$") ~= nil)
    local ok, ret
    if is_short_code then
        ok, ret = pcall(sms.send, to, text, false)
    else
        ok, ret = pcall(sms.send, to, text)
    end
    log.info("sms", "sms.send to:", to, "len:", #text, "short:", is_short_code, "ok:", ok, "ret:", ret)
    return ok and (ret == true)
end

local function dispatch_next()
    if active_tx or sms_frozen then return end
    local req = table.remove(pending_tx, 1)
    if not req then return end
    active_tx = req
    if not execute_sms_send(req.to, req.text) then
        active_tx = nil
        serial_comm.send_response(req.id, -102, "SEND_FAILED", { reason = "modem_rejected" })
        dispatch_next()
    else
        active_tx = nil
        sms_frozen = false
        serial_comm.send_response(req.id, 0, "SENT_OK", { success = true })
        dispatch_next()
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
        serial_comm.publish("sms_rx", {
            id = msg_id,
            from = from,
            content = content,
            code = code,
            time = timestamp,
            bsp = model.bsp(),
            model = model.bsp(),
            imei = model.imei(),
            iccid = model.iccid()
        })
        sys.publish("NOTIFY_PUSH", "sms", from, content, code, msg_id)

        -- 双模自适应存储策略：若开启了仅存上位机(store_on_board==0)且串口在线，走 3.5s ACK 兜底；否则直接存盘
        local store_policy = (fskv and fskv.get("store_on_board"))
        if store_policy == nil then store_policy = 1 end

        if store_policy == 0 and serial_comm.is_connected and serial_comm.is_connected() then
            log.info("sms", "Store-on-host active & serial connected, buffering in RAM with 3.5s fallback:", msg_id)
            local timer_id = sys.timerStart(function()
                if pending_store_sms[msg_id] then
                    pending_store_sms[msg_id] = nil
                    log.warn("sms", "Store ACK timeout 3.5s for msg:", msg_id, "fallback to LittleFS")
                    sys.publish("SMS_SAVE", from, content, code, msg_id, timestamp)
                end
            end, 3500)
            pending_store_sms[msg_id] = {
                timer_id = timer_id,
                from = from,
                content = content,
                code = code,
                timestamp = timestamp
            }
        else
            sys.publish("SMS_SAVE", from, content, code, msg_id, timestamp)
        end
    end)
    sys.subscribe("SMS_STORE_ACK", function(ack_id)
        if not ack_id then return end
        local rec = pending_store_sms[ack_id]
        if rec then
            if rec.timer_id then sys.timerStop(rec.timer_id) end
            pending_store_sms[ack_id] = nil
            log.info("sms", "Store ACK confirmed for msg:", ack_id, "- 0 Flash wear fulfilled")
        end
    end)
    sys.subscribe("SMS_SENT", function(result)
        log.info("sms", "SMS_SENT result:", result and "ok" or "failed")
        serial_comm.publish("sms_debug", {
            stage = "sms_sent_event",
            result = result
        })
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
            local to = data.to or data.phone or cmd_packet.phone or cmd_packet.to
            local text = data.text or data.content or cmd_packet.content or cmd_packet.text
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
                if not execute_sms_send(to, text) then
                    active_tx = nil
                    serial_comm.send_response(req_id, -102, "SEND_FAILED", { reason = "modem_rejected" })
                    dispatch_next()
                else
                    active_tx = nil
                    sms_frozen = false
                    serial_comm.send_response(req_id, 0, "SENT_OK", { success = true })
                    dispatch_next()
                end
            end
        elseif cmd_packet.cmd == "reset_sms" or cmd_packet.cmd == "clear_sms_queue" then
            local req_id = cmd_packet.id or ("reset_" .. os.time())
            if active_tx and active_tx.timer_id then
                sys.timerStop(active_tx.timer_id)
            end
            active_tx = nil
            sms_frozen = false
            pending_tx = {}
            serial_comm.send_response(req_id, 0, "OK", { status = "sms_queue_cleared" })
            return
        end
    end)
    log.info("sms", "init ok")
end
return M
