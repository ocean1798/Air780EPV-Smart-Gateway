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
function model.os()
    return (rtos and rtos.firmware and rtos.firmware()) or "unknown"
end
function model.bsp()
    return (hmeta and hmeta.model and hmeta.model()) or (rtos and rtos.bsp and rtos.bsp()) or "unknown"
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
    return mobile and mobile.sn and mobile.sn() or nil
end
function model.imei()
    return mobile and mobile.imei and mobile.imei() or nil
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
