local M = {}
local uart_id = (config and config.serial and config.serial.id) or uart.VUART_0
local rx_cache = ""
local session_id = string.format("boot_%d_%d", os.time(), math.random(1000, 9999))
local event_sequence = 0
local MAX_FRAME_BYTES = 65536
local last_rx_time = 0

function M.is_connected()
    return (os.time() - last_rx_time) < 10
end

local function next_event_id(event_name)
    event_sequence = event_sequence + 1
    return string.format("%s_%s_%d", session_id, event_name or "event", event_sequence)
end
M.new_event_id = next_event_id

local function command_log_summary(packet)
    if type(packet) ~= "table" then return "invalid" end
    local params = packet.params or packet.data
    local keys = {}
    if type(params) == "table" then
        for key, _ in pairs(params) do
            table.insert(keys, tostring(key))
        end
        table.sort(keys)
    end
    return string.format("type=%s id=%s cmd=%s param_keys=%s",
        tostring(packet.type or ""), tostring(packet.id or ""), tostring(packet.cmd or ""), table.concat(keys, ","))
end
function M.send(data_table)
    if not data_table or type(data_table) ~= "table" then return false end
    local ok, json_str = pcall(json.encode, data_table)
    if not ok or not json_str then
        log.error("serial", "json.encode failed")
        return false
    end
    local written = uart.write(uart_id, json_str .. "\r\n")
    log.info("serial", "uart.write to id:", uart_id, "bytes:", written)
    return true
end
function M.publish(event_name, data)
    local event_id = next_event_id(event_name)
    if type(data) == "table" and not data.id then data.id = event_id end
    return M.send({
        type = "event",
        id = event_id,
        event = event_name,
        data = data or {},
        ts = os.time()
    })
end
function M.send_response(req_id, code, msg, data)
    return M.send({
        type = "res",
        id = req_id or "unknown",
        code = code or 0,
        msg = msg or "OK",
        data = data
    })
end
function M.init()
    -- A persisted boot sequence prevents equal timestamps from reusing SMS IDs.
    if fskv then
        local raw_seq = fskv.get("gateway_boot_seq")
        local boot_sequence = (raw_seq and tonumber(raw_seq) or 0) + 1
        if fskv.set("gateway_boot_seq", boot_sequence) then
            session_id = string.format("boot_%d_%d", boot_sequence, os.time())
        end
    end
    uart.setup(uart_id, 115200, 8, 1)
    uart.on(uart_id, "receive", function(id, len)
        local data = uart.read(id, len)
        if not data or #data == 0 then return end
        rx_cache = rx_cache .. data
        while true do
            local pos = rx_cache:find("\n")
            if not pos then break end
            local raw_line = rx_cache:sub(1, pos)
            rx_cache = rx_cache:sub(pos + 1)
            local line = raw_line:match("^%s*(.-)%s*$")
            if #line > 0 then
                local succ, obj = false, nil
                if #line <= MAX_FRAME_BYTES then succ, obj = pcall(json.decode, line) end
                if succ and type(obj) == "table" then
                    last_rx_time = os.time()
                    log.info("serial", "CMD received:", command_log_summary(obj))
                    if obj.type == "cmd" and obj.cmd == "ping" then
                        M.send_response(obj.id, 0, "pong", { time = os.time() })
                    elseif obj.type == "cmd" and obj.cmd == "sms_store_ack" then
                        local ack_id = (obj.data and obj.data.id) or (obj.params and obj.params.id) or obj.id
                        log.info("serial", "sms_store_ack received for:", ack_id)
                        sys.publish("SMS_STORE_ACK", ack_id)
                    elseif obj.type == "cmd" then
                        sys.publish("SERIAL_CMD", obj)
                    else
                        log.warn("serial", "Ignored non-command frame type:", tostring(obj.type or "unknown"))
                    end
                else
                    log.warn("serial", "Invalid JSON packet bytes:", #line)
                end
            end
        end
        if #rx_cache > MAX_FRAME_BYTES then
            log.warn("serial", "rx_cache overflow, reset buffer")
            rx_cache = ""
        end
    end)
    log.info("serial", "VUART initialized on id:", uart_id)
    local test_ret = uart.write(uart_id, "{\"event\":\"boot_handshake\"}\r\n")
    log.info("serial", "Initial handshake write ret:", test_ret)
end
return M
