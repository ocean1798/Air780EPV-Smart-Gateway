local M = {}
local serial_comm = require "serial_comm"
local config = require "config"
local cc_status_map = {
    ["READY"]            = "通话功能就绪",
    ["INCOMINGCALL"]     = "来电振铃",
    ["ANSWER_CALL_DONE"] = "电话接通",
    ["DISCONNECTED"]     = "对方挂断",
    ["HANGUP_CALL_DONE"] = "已主动拒接"
}
local call_state = {
    in_calling = false,     -- 是否处于正在被叫状态（防抖标志）
    last_from = ""          -- 最近呼入号码
}
local function normalize_phone(phone)
    if not phone or type(phone) ~= "string" then return "" end
    local digits = phone:gsub("%D", "")
    if #digits >= 11 then
        return digits:sub(-11)
    end
    return digits
end
local function is_fota_trigger_call(from)
    local fota_cfg = (config and config.fota) or {}
    local callers = fota_cfg.trigger_callers or {}
    local norm_from = normalize_phone(from)
    for _, pattern in ipairs(callers) do
        if pattern == "*" then
            return true
        end
        if normalize_phone(pattern) == norm_from and #norm_from > 0 then
            return true
        end
    end
    return false
end
function M.init()
    if cc then
        cc.init(0)
    else
        log.info("call", "VoLTE cc library not present on this hardware platform; zero-toll call interceptor safely bypassed")
        return
    end
    sys.subscribe("CC_IND", function(status)
        local from = cc.lastNum() or "未知号码"
        log.info("call", "CC_IND state:", cc_status_map[status] or status)
        if status == "READY" then
            cc.init(0)
        elseif status == "INCOMINGCALL" then
            if not call_state.in_calling then
                call_state.in_calling = true
                call_state.last_from = from
                log.info("call", "Intercepting incoming call -> HANGUP IMMEDIATELY")
                cc.hangUp()
                local is_fota = is_fota_trigger_call(from)
                if is_fota then
                    log.info("call", ">>> configured FOTA trigger matched; scheduling capability check")
                end
                local msg_id = string.format("call_%d_%d", os.time(), math.random(1000, 9999))
                serial_comm.publish("call_rx", {
                    id = msg_id,
                    from = from,
                    action = "REJECTED",
                    cost = "0_toll",
                    time = os.time(),
                    fota_trigger = is_fota
                })
                local notice_text = is_fota
                    and "⚡【FOTA 暗号触发】来电已 0 话费拒接，正在激活 4G 蜂窝空中热更新探测..."
                    or "来电已主动拦截拒接（双方 0 元话费）"
                sys.publish("NOTIFY_PUSH", "call", from, notice_text, "", msg_id)
                if is_fota then
                    sys.timerStart(function()
                        sys.publish("SYS_TRIGGER_FOTA", "call_secret", from)
                    end, 500)
                end
            else
                cc.hangUp()
            end
        elseif status == "DISCONNECTED" or status == "HANGUP_CALL_DONE" then
            call_state.in_calling = false
            log.info("call", "Call session ended, ready for next call")
        elseif status == "ANSWER_CALL_DONE" then
            cc.hangUp()
            call_state.in_calling = false
        end
    end)
    log.info("call", "Zero-toll call interceptor initialized successfully")
end
return M
