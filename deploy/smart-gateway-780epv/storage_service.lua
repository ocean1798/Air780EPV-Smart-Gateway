local storage = {}
local BOX_FILE = "/sms_box.jsonl"
local MAX_RECORDS = 100
local PRUNE_COUNT = 20
local current_count = 0
local history_generation = 0
local function scan_record_count()
    local f = io.open(BOX_FILE, "r")
    if not f then
        current_count = 0
        return 0
    end
    local count = 0
    for _ in f:lines() do
        count = count + 1
    end
    f:close()
    current_count = count
    history_generation = count
    return count
end
local function prune_old_records()
    log.info("storage", "Pruning old SMS records, current count:", current_count)
    local f = io.open(BOX_FILE, "r")
    if not f then return end
    local lines = {}
    for line in f:lines() do
        table.insert(lines, line)
    end
    f:close()
    local prune_target = math.min(#lines, PRUNE_COUNT)
    for i = 1, prune_target do
        table.remove(lines, 1)
    end
    local fw = io.open(BOX_FILE, "w")
    if fw then
        for _, l in ipairs(lines) do
            fw:write(l .. "\n")
        end
        fw:close()
    end
    current_count = #lines
    history_generation = history_generation + 1
    lines = nil
    collectgarbage("collect")
    log.info("storage", "Prune complete, new count:", current_count)
end
function storage.append_sms(sender, content, otp_code, timestamp, msg_id)
    if current_count >= MAX_RECORDS then
        prune_old_records()
    end
    local record = {
        sender = sender or "",
        content = content or "",
        otp = otp_code or "",
        time = timestamp or os.time(),
        id = msg_id
    }
    local ok, json_str = pcall(json.encode, record)
    if not ok or not json_str then return false end
    local f = io.open(BOX_FILE, "a+")
    if f then
        f:write(json_str .. "\n")
        f:close()
        current_count = current_count + 1
        history_generation = history_generation + 1
        return true
    else
        log.error("storage", "Failed to open BOX_FILE for append")
        return false
    end
end
function storage.stream_records(emitter_fn, limit, cursor)
    collectgarbage("collect")
    local f = io.open(BOX_FILE, "r")
    if not f then
        emitter_fn(nil, 0)
        return
    end
    local raw_lines = {}
    for line in f:lines() do
        if line and #line > 2 then
            table.insert(raw_lines, line)
        end
    end
    f:close()
    local total = #raw_lines
    local offset = 0
    if cursor and #cursor > 0 then
        local generation, parsed_offset = cursor:match("^h:(%d+):(%d+)$")
        if not generation or tonumber(generation) ~= history_generation then
            raw_lines = nil
            collectgarbage("collect")
            emitter_fn(nil, total, nil, "CURSOR_STALE")
            return
        end
        offset = tonumber(parsed_offset) or 0
    end
    -- 单次硬顶限制 15 条，杜绝 128KB RAM 碎片化导致的 OOM
    local page_size = tonumber(limit) or 15
    if page_size < 1 then page_size = 15 end
    if page_size > 15 then page_size = 15 end

    local end_index = total - offset
    local start_index = math.max(1, end_index - page_size + 1)
    local paged = {}
    if end_index >= 1 then
        for i = start_index, end_index do
            local ok, obj = pcall(json.decode, raw_lines[i])
            if ok and obj then
                table.insert(paged, obj)
            end
        end
    end
    raw_lines = nil
    local consumed = #paged
    local next_cursor = start_index > 1 and string.format("h:%d:%d", history_generation, offset + consumed) or nil
    emitter_fn(paged, total, next_cursor, nil)
    paged = nil
    collectgarbage("collect")
end
function storage.get_history_generation()
    return history_generation
end
function storage.get_count()
    return current_count
end
function storage.clear()
    os.remove(BOX_FILE)
    current_count = 0
    history_generation = history_generation + 1
    collectgarbage("collect")
    log.info("storage", "SMS Blackbox cleared")
    return true
end
function storage.init()
    scan_record_count()
    log.info("storage", "SMS Blackbox initialized, existing records:", current_count)
    sys.subscribe("SMS_SAVE", function(sender, content, otp_code, msg_id, timestamp)
        storage.append_sms(sender, content, otp_code, timestamp or os.time(), msg_id)
    end)
    sys.subscribe("SERIAL_CMD", function(cmd_packet)
        if not cmd_packet or type(cmd_packet) ~= "table" then return end
        local serial_comm = require "serial_comm"
        local req_id = cmd_packet.id or ("st_" .. os.time())
        if cmd_packet.cmd == "get_history" then
            local data = cmd_packet.data or cmd_packet.params or {}
            local limit = tonumber(data.limit) or 15
            storage.stream_records(function(records, total, next_cursor, error_code)
                if error_code then
                    serial_comm.send_response(req_id, -409, error_code, { generation = history_generation })
                    return
                end
                serial_comm.send_response(req_id, 0, "HISTORY_OK", {
                    total = total,
                    count = records and #records or 0,
                    items = records or {},
                    generation = history_generation,
                    next_cursor = next_cursor
                })
            end, limit, data.cursor)
        elseif cmd_packet.cmd == "clear_history" then
            storage.clear()
            serial_comm.send_response(req_id, 0, "HISTORY_CLEARED", { total = 0, generation = history_generation })
        end
    end)
end
return storage
