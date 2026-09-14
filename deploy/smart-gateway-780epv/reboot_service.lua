local M = {}
local config = require "config"
local serial_comm = require "serial_comm"
local start_ts = os.time()
local active_hour = -1

local function get_uptime()
    if mcu and mcu.ticks then
        local t = mcu.ticks()
        if type(t) == "number" then return math.floor(t / 1000) end
    end
    return math.max(0, os.time() - start_ts)
end

local function get_configured_hour()
    if fskv then
        local s = fskv.get("daily_reboot_hour")
        if type(s) == "number" then return s end
    end
    if config and config.system and config.system.daily_reboot_hour ~= nil then
        return tonumber(config.system.daily_reboot_hour) or -1
    end
    return -1
end

function M.get_status()
    local uptime = get_uptime()
    local now_t = os.date("*t")
    local synced = (now_t and type(now_t.year) == "number" and now_t.year >= 2024)
    local cur_time = synced and string.format("%04d-%02d-%02d %02d:%02d:%02d",
        now_t.year, now_t.month, now_t.day, now_t.hour, now_t.min, now_t.sec) or "未同步基站时钟"
    local desc, sec = "未开启", -1

    if active_hour >= 0 and active_hour <= 23 then
        if not synced then
            desc = string.format("每天 %02d:00 (等待对时)", active_hour)
        else
            local cur_sec = now_t.hour * 3600 + now_t.min * 60 + now_t.sec
            local tgt_sec = active_hour * 3600
            local today = string.format("%04d%02d%02d", now_t.year, now_t.month, now_t.day)
            local last_day = fskv and fskv.get("last_reboot_day") or ""
            if tgt_sec > cur_sec and last_day ~= today then
                sec = tgt_sec - cur_sec
                desc = string.format("今天 %02d:00", active_hour)
            else
                sec = (86400 - cur_sec) + tgt_sec
                desc = string.format("明天 %02d:00", active_hour)
            end
        end
    end

    return {
        uptime_seconds = uptime,
        daily_reboot_hour = active_hour,
        time_synced = synced,
        current_time = cur_time,
        next_reboot_desc = desc,
        next_reboot_seconds = sec
    }
end

function M.set_policy(hour_input)
    local n = tonumber(hour_input)
    active_hour = (n and n >= 0 and n <= 23) and math.floor(n) or -1
    if fskv then fskv.set("daily_reboot_hour", active_hour) end
    log.info("reboot", "Daily reboot set to:", active_hour)
    return M.get_status()
end

function M.trigger_reboot(reason, delay_ms)
    local delay = delay_ms or 1500
    log.warn("reboot", "Reboot triggered:", reason)
    serial_comm.publish("gateway_rebooting", {
        reason = reason or "manual",
        delay_ms = delay,
        uptime = get_uptime()
    })
    sys.publish("NOTIFY_PUSH", "reboot", "", "智能网关即将重启: " .. tostring(reason))
    sys.timerStart(rtos.reboot, delay)
end

function M.init()
    start_ts = os.time()
    active_hour = get_configured_hour()
    log.info("reboot", "Init daily reboot hour:", active_hour)
    sys.taskInit(function()
        while true do
            sys.wait(30000)
            if active_hour >= 0 and active_hour <= 23 then
                local now_t = os.date("*t")
                if now_t and type(now_t.year) == "number" and now_t.year >= 2024 and now_t.hour == active_hour then
                    local today = string.format("%04d%02d%02d", now_t.year, now_t.month, now_t.day)
                    local last = fskv and fskv.get("last_reboot_day") or ""
                    if last ~= today then
                        if fskv then fskv.set("last_reboot_day", today) end
                        log.warn("reboot", "Scheduled reboot hour reached:", active_hour)
                        M.trigger_reboot("daily_scheduled_reboot", 2000)
                        break
                    end
                end
            end
        end
    end)
end

return M
