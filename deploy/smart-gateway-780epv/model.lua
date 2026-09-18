local model = {}
local temp_is_valid, vbat_is_valid = false, false
local function read_adc(channel)
    if not adc or not channel then return nil, false end
    local open_ok, opened = pcall(adc.open, channel)
    if not open_ok or opened == false then return nil, false end
    local ok, raw = pcall(adc.get, channel)
    pcall(adc.close, channel)
    -- adc.get returns -1 on failure; CPU millidegrees may be zero/negative.
    if not ok or type(raw) ~= "number" or raw ~= raw or raw == -1
        or (channel == adc.CH_VBAT and raw <= 0) then return nil, false end
    return string.format("%.2f", raw / 1000), true
end
function model.temp()
    local value, valid = read_adc(adc and adc.CH_CPU)
    temp_is_valid = valid
    return value
end
function model.vbat()
    local value, valid = read_adc(adc and adc.CH_VBAT)
    vbat_is_valid = valid
    return value
end
function model.temp_valid()
    return temp_is_valid
end
function model.vbat_valid()
    return vbat_is_valid
end
local cached_bsp = nil
local cached_imei = nil
local cached_sn = nil
local cached_iccid = nil
local cached_mcu_id = nil

function model.os()
    return (rtos and rtos.firmware and rtos.firmware()) or "unknown"
end
function model.bsp()
    if cached_bsp then return cached_bsp end
    cached_bsp = (hmeta and hmeta.model and hmeta.model()) or (rtos and rtos.bsp and rtos.bsp()) or "unknown"
    return cached_bsp
end
function model.hw()
    return (hmeta and hmeta.hwver and hmeta.hwver()) or "unknown"
end
function model.chip()
    return (hmeta and hmeta.chip and hmeta.chip()) or "unknown"
end
function model.build()
    return rtos and rtos.buildDate and rtos.buildDate() or "unknown"
end
function model.sn()
    if cached_sn and #cached_sn > 0 then return cached_sn end
    if mobile and mobile.sn then
        local ok, val = pcall(mobile.sn)
        if ok and val and #val > 0 then
            cached_sn = val
            return cached_sn
        end
    end
    return nil
end
function model.imei()
    if cached_imei and #cached_imei > 0 then return cached_imei end
    if mobile and mobile.imei then
        local ok, val = pcall(mobile.imei)
        if ok and val and #val > 0 then
            cached_imei = val
            return cached_imei
        end
    end
    return nil
end
function model.iccid()
    if cached_iccid and #cached_iccid > 0 then return cached_iccid end
    if mobile and mobile.iccid then
        local ok, val = pcall(mobile.iccid)
        if ok and val and #val > 0 then
            cached_iccid = val
            return cached_iccid
        end
    end
    return nil
end
function model.mcu_id()
    if cached_mcu_id then return cached_mcu_id end
    if mcu and mcu.unique_id then
        local ok, val = pcall(mcu.unique_id)
        if ok and val and type(val) == "string" and val.toHex then
            cached_mcu_id = val:toHex()
            return cached_mcu_id
        end
    end
    return nil
end
function model.device_label()
    local b = model.bsp()
    local m = model.imei()
    if m and #m > 0 then
        return string.format("%s (IMEI: %s)", b, m)
    end
    return b
end
function model.capabilities()
    return {
        bsp = model.bsp(),
        chip = model.chip(),
        has_volte = (cc ~= nil),
        has_fota = (fota ~= nil and type(fota.init) == "function"),
        has_adc_cpu = (adc and adc.CH_CPU ~= nil) == true,
        has_adc_vbat = (adc and adc.CH_VBAT ~= nil) == true
    }
end
return model
