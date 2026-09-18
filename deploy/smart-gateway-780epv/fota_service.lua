local M = {}
local serial_comm = require "serial_comm"
local config = require "config"
local FOTA_CAPABILITY = fota and type(fota.init) == "function"
    and type(fota.file) == "function" and type(fota.isDone) == "function"
    and type(fota.finish) == "function" and type(fota.wait) == "function"
    and "supported" or "unsupported"
local fota_state = { is_running = false, last_trigger_time = 0, last_source = "none", last_caller = "" }
local function parse_version(v)
    if not v or type(v) ~= "string" then return {0, 0, 0} end
    local parts = {}
    for num in v:gsub("^[vV]", ""):gmatch("(%d+)") do table.insert(parts, tonumber(num) or 0) end
    while #parts < 3 do table.insert(parts, 0) end
    return parts
end
local function is_newer_version(remote, local_v)
    local r = parse_version(remote)
    local l = parse_version(local_v)
    for i = 1, 3 do
        if r[i] > l[i] then return true end
        if r[i] < l[i] then return false end
    end
    return false
end
local function execute_fota_task(source, caller, custom_url)
    if fota_state.is_running then return end
    fota_state.is_running = true
    fota_state.last_trigger_time = os.time()
    fota_state.last_source = source or "unknown"
    fota_state.last_caller = caller or ""
    log.info("fota", "start", source)
    local watchdog = sys.timerStart(function()
        if fota_state.is_running then
            log.error("fota", "timeout 120s")
            fota_state.is_running = false
            if _G.set_temp_cellular_data then _G.set_temp_cellular_data(false, "timeout") end
            serial_comm.publish("fota_status", { status = "failed", error = "TIMEOUT" })
        end
    end, 120000)
    if _G.set_temp_cellular_data then _G.set_temp_cellular_data(true, "fota") end
    local count = 0
    while count < 10 do
        if mobile and mobile.status and mobile.status() == 1 then break end
        sys.wait(1000)
        count = count + 1
    end
    local local_ver = _G.GATEWAY_VERSION or "1.2.9"
    serial_comm.publish("fota_status", { status = "checking", source = source, caller = caller, current_version = local_ver })
    local fota_cfg = (config and config.fota) or {}
    local version_url = custom_url or (fskv and fskv.get("fota_version_url")) or fota_cfg.version_url or "http://your-bucket-domain.clouddn.com/version.json"
    local my_bsp = (rtos and rtos.bsp and rtos.bsp()) or "EC718P"
    local query_sep = version_url:find("?") and "&" or "?"
    local target_ver_url = version_url .. query_sep .. "bsp=" .. my_bsp .. "&v=" .. local_ver
    local code, headers, body = http.request("GET", target_ver_url, nil, nil, { timeout = 15000 }).wait()
    if code ~= 200 or not body or #body == 0 then
        log.warn("fota", "ver manifest err", code)
        serial_comm.publish("fota_status", { status = "failed", error = "MANIFEST_ERR", http_code = code })
        sys.publish("NOTIFY_PUSH", "fota", caller or "System", "❌ FOTA 探测失败: 无法拉取版本文件 (HTTP " .. tostring(code) .. ")")
        sys.timerStop(watchdog)
        if _G.set_temp_cellular_data then _G.set_temp_cellular_data(false, "manifest_err") end
        fota_state.is_running = false
        return
    end
    local succ, meta = pcall(json.decode, body)
    if not succ or type(meta) ~= "table" or not meta.version then
        log.warn("fota", "parse err")
        serial_comm.publish("fota_status", { status = "failed", error = "PARSE_ERR" })
        sys.timerStop(watchdog)
        if _G.set_temp_cellular_data then _G.set_temp_cellular_data(false, "parse_err") end
        fota_state.is_running = false
        return
    end
    local remote_ver = meta.version
    log.info("fota", "ver check", local_ver, "->", remote_ver)
    if not is_newer_version(remote_ver, local_ver) then
        log.info("fota", "up to date")
        serial_comm.publish("fota_status", { status = "up_to_date", remote_version = remote_ver, current_version = local_ver })
        sys.publish("NOTIFY_PUSH", "fota", caller or "System", "网关固件已是最新版本 (v" .. local_ver .. ")，无需升级。\r\n更新日志: " .. (meta.changelog or "常规维护"))
        sys.timerStop(watchdog)
        if _G.set_temp_cellular_data then _G.set_temp_cellular_data(false, "up_to_date") end
        fota_state.is_running = false
        return
    end
    local bin_url = nil
    if meta.platforms and type(meta.platforms) == "table" and meta.platforms[my_bsp] then
        bin_url = meta.platforms[my_bsp].url
    end
    bin_url = bin_url or meta.url or fota_cfg.default_bin_url or "http://your-bucket-domain.clouddn.com/script.bin"
    log.info("fota", "downloading configured artifact")
    serial_comm.publish("fota_status", { status = "downloading", target_version = remote_ver, current_version = local_ver, size = meta.size })
    sys.publish("NOTIFY_PUSH", "fota", caller or "System", "🚀 发现新固件 v" .. remote_ver .. " (当前 v" .. local_ver .. ")\r\n更新: " .. (meta.changelog or "常规优化") .. "\r\n\r\n正在拉取升级包并刷写 Flash，完成后自动重启生效，请勿断电！")
    sys.wait(1000)

    local temp_path = "/update.bin"
    if io.exists(temp_path) then os.remove(temp_path) end
    local dl_code = http.request("GET", bin_url, nil, nil, { dst = temp_path, timeout = 60000 }).wait()
    local is_fota_ok = false
    local err_reason = "DOWNLOAD_FAIL"

    if dl_code == 200 then
        local file_size = io.fileSize(temp_path) or 0
        if file_size > 0 then
            local md5_ok = true
            if meta.md5 and #meta.md5 > 0 and crypto and crypto.md_file then
                local ok, hash = pcall(crypto.md_file, "MD5", temp_path)
                if ok and hash and hash:lower() ~= meta.md5:lower() then
                    log.error("fota", "md5 mismatch", hash, meta.md5)
                    md5_ok = false
                    err_reason = "MD5_MISMATCH"
                end
            end
            if md5_ok then
                if fota and fota.init and fota.init() then
                    local t_start = os.clock()
                    local wait_ok = true
                    while not fota.wait() do
                        if os.clock() - t_start > 30 then wait_ok = false break end
                        sys.wait(100)
                    end
                    if wait_ok then
                        local result = fota.file(temp_path)
                        if result then
                            while true do
                                local succ, done = fota.isDone()
                                if not succ then fota.finish(false) err_reason = "FLASH_FAIL" break end
                                if done then fota.finish(true) is_fota_ok = true break end
                                sys.wait(200)
                            end
                        else
                            fota.finish(false)
                            err_reason = "FOTA_FILE_ERR"
                        end
                    else
                        fota.finish(false)
                        err_reason = "FOTA_TIMEOUT"
                    end
                else
                    err_reason = "FOTA_INIT_ERR"
                end
            end
        else
            err_reason = "EMPTY_FILE"
        end
    else
        err_reason = "HTTP_" .. tostring(dl_code)
    end

    if io.exists(temp_path) then os.remove(temp_path) end
    sys.timerStop(watchdog)

    if is_fota_ok then
        log.info("fota", "success, rebooting")
        if fskv then fskv.set("fota_just_updated", remote_ver) end
        serial_comm.publish("fota_status", { status = "reboot_pending", target_version = remote_ver, current_version = local_ver, reboot_in = 2000 })
        sys.wait(2000)
        rtos.reboot()
    else
        log.error("fota", "fail", err_reason, dl_code)
        serial_comm.publish("fota_status", { status = "failed", error = err_reason, http_code = dl_code })
        sys.publish("NOTIFY_PUSH", "fota", caller or "System", "❌ FOTA 固件升级失败: " .. err_reason .. "，维持当前版本。")
        if _G.set_temp_cellular_data then _G.set_temp_cellular_data(false, "fail") end
        fota_state.is_running = false
    end
end
function M.trigger(source, caller, custom_url)
    if FOTA_CAPABILITY ~= "supported" then
        serial_comm.publish("fota_status", { status = "unsupported", capability = FOTA_CAPABILITY, reason = "runtime_api_unavailable" })
        return false
    end
    sys.taskInit(execute_fota_task, source or "manual", caller or "", custom_url)
    return true
end
function M.get_status()
    return { capability = FOTA_CAPABILITY, version_confirmed = false, is_running = fota_state.is_running, last_trigger_time = fota_state.last_trigger_time, last_source = fota_state.last_source, last_caller = fota_state.last_caller, version = _G.GATEWAY_VERSION or "unknown" }
end
local serial_ota_state = {
    is_active = false,
    expected_size = 0,
    expected_md5 = "",
    total_chunks = 0,
    received_chunks = 0,
    received_bytes = 0,
    file_path = "/update.sota",
    fd = nil
}

function M.handle_serial_ota(cmd_packet)
    local cmd = cmd_packet.cmd
    local data = cmd_packet.data or cmd_packet.params or {}
    local req_id = cmd_packet.id

    if cmd == "ota_start" then
        if serial_ota_state.is_active and serial_ota_state.fd then
            io.close(serial_ota_state.fd)
            serial_ota_state.fd = nil
        end
        if io.exists(serial_ota_state.file_path) then
            os.remove(serial_ota_state.file_path)
        end
        local fd = io.open(serial_ota_state.file_path, "wb")
        if not fd then
            return serial_comm.send_response(req_id, -1001, "FILE_OPEN_FAILED", { error = "Cannot open /update.sota" })
        end
        serial_ota_state.is_active = true
        serial_ota_state.fd = fd
        serial_ota_state.expected_size = tonumber(data.size) or 0
        serial_ota_state.expected_md5 = tostring(data.md5 or ""):lower()
        serial_ota_state.total_chunks = tonumber(data.total_chunks) or 0
        serial_ota_state.received_chunks = 0
        serial_ota_state.received_bytes = 0
        log.info("fota", "Serial OTA started, size:", serial_ota_state.expected_size, "chunks:", serial_ota_state.total_chunks)
        return serial_comm.send_response(req_id, 0, "OTA_READY", {
            ready = true,
            chunk_size = 2048
        })

    elseif cmd == "ota_chunk" then
        if not serial_ota_state.is_active or not serial_ota_state.fd then
            return serial_comm.send_response(req_id, -1002, "NOT_IN_OTA", { error = "No active serial OTA session" })
        end
        local chunk_idx = tonumber(data.index) or 0
        local b64_payload = data.data or ""
        if #b64_payload == 0 then
            return serial_comm.send_response(req_id, -1003, "EMPTY_PAYLOAD", { error = "Chunk payload is empty" })
        end
        local bin_chunk = crypto and crypto.base64_decode and crypto.base64_decode(b64_payload)
        if not bin_chunk then
            return serial_comm.send_response(req_id, -1004, "BASE64_DECODE_ERR", { error = "Failed to decode chunk" })
        end
        local write_ok = serial_ota_state.fd:write(bin_chunk)
        if not write_ok then
            return serial_comm.send_response(req_id, -1005, "WRITE_FAILED", { error = "Failed to write chunk to flash" })
        end
        serial_ota_state.received_chunks = serial_ota_state.received_chunks + 1
        serial_ota_state.received_bytes = serial_ota_state.received_bytes + #bin_chunk
        return serial_comm.send_response(req_id, 0, "CHUNK_ACK", {
            index = chunk_idx,
            received_bytes = serial_ota_state.received_bytes
        })

    elseif cmd == "ota_finish" then
        if not serial_ota_state.is_active or not serial_ota_state.fd then
            return serial_comm.send_response(req_id, -1002, "NOT_IN_OTA", { error = "No active serial OTA session" })
        end
        io.close(serial_ota_state.fd)
        serial_ota_state.fd = nil
        serial_ota_state.is_active = false

        -- 必须在协程中执行 Flash 写入与等待，避免在回调上下文调用 sys.wait() 产生异常
        sys.taskInit(function()
            local file_path = serial_ota_state.file_path
            local actual_md5 = ""
            if crypto and crypto.md_file then
                local ok, h = pcall(crypto.md_file, "MD5", file_path)
                if ok and h then actual_md5 = h:lower() end
            end

            if serial_ota_state.expected_md5 ~= "" and actual_md5 ~= "" and actual_md5 ~= serial_ota_state.expected_md5 then
                log.error("fota", "MD5 mismatch:", actual_md5, "vs", serial_ota_state.expected_md5)
                os.remove(file_path)
                return serial_comm.send_response(req_id, -1006, "MD5_MISMATCH", {
                    expected = serial_ota_state.expected_md5,
                    actual = actual_md5
                })
            end

            log.info("fota", "Starting FOTA burn from:", file_path)
            if fota and fota.init and fota.init() then
                local t_start = os.clock()
                local wait_ok = true
                while not fota.wait() do
                    if os.clock() - t_start > 30 then wait_ok = false break end
                    sys.wait(100)
                end
                if wait_ok then
                    local res = fota.file(file_path)
                    if res then
                        local burn_succ = false
                        while true do
                            local succ, done = fota.isDone()
                            if not succ then fota.finish(false) break end
                            if done then fota.finish(true) burn_succ = true break end
                            sys.wait(200)
                        end
                        if burn_succ then
                            os.remove(file_path)
                            serial_comm.send_response(req_id, 0, "UPGRADE_SUCCESS", {
                                will_reboot = true,
                                msg = "FOTA burn complete, module will reboot in 1.5s"
                            })
                            sys.timerStart(function()
                                log.info("fota", "Rebooting module to apply updated firmware...")
                                if rtos and rtos.reboot then rtos.reboot() end
                            end, 1500)
                            return
                        else
                            fota.finish(false)
                        end
                    else
                        fota.finish(false)
                    end
                else
                    fota.finish(false)
                end
            end

            os.remove(file_path)
            serial_comm.send_response(req_id, -1007, "FOTA_FLASH_FAILED", {
                error = "Underlying fota.file failed to write Flash"
            })
        end)
        return
    elseif cmd == "ota_abort" then
        if serial_ota_state.fd then
            io.close(serial_ota_state.fd)
            serial_ota_state.fd = nil
        end
        serial_ota_state.is_active = false
        if io.exists(serial_ota_state.file_path) then
            os.remove(serial_ota_state.file_path)
        end
        return serial_comm.send_response(req_id, 0, "ABORTED", { ok = true })
    end
end

function M.init()
    sys.subscribe("SYS_TRIGGER_FOTA", function(source, caller, custom_url) M.trigger(source, caller, custom_url) end)
    sys.subscribe("SERIAL_CMD", function(cmd_packet)
        if not cmd_packet or type(cmd_packet) ~= "table" then return end
        if cmd_packet.cmd == "trigger_fota" or cmd_packet.cmd == "check_fota" then
            local data = cmd_packet.data or cmd_packet.params or {}
            local accepted = M.trigger("serial_cmd", data.caller or "host_pc", data.url)
            serial_comm.send_response(cmd_packet.id, accepted and 0 or -409, accepted and "FOTA_TRIGGERED" or "FOTA_CAPABILITY_UNKNOWN", M.get_status())
        elseif cmd_packet.cmd == "get_fota_status" then
            serial_comm.send_response(cmd_packet.id, 0, "OK", M.get_status())
        elseif cmd_packet.cmd == "ota_start" or cmd_packet.cmd == "ota_chunk" or cmd_packet.cmd == "ota_finish" or cmd_packet.cmd == "ota_abort" then
            M.handle_serial_ota(cmd_packet)
        end
    end)
    log.info("fota", "init v" .. (_G.GATEWAY_VERSION or "1.2.9"))
end
return M
