local led = {}
local default_cfg = {
    network = { gpio = 30 },
    event   = { gpio = 27, total = 5, wait = 60 }
}
local cfg = (config and config.system and config.system.led) or default_cfg
local event   = gpio.setup(cfg.event.gpio, 0)
local network = gpio.setup(cfg.network.gpio, 1)
local state = {
    network = nil,
    event   = 0
}
function led.network(x)
    x = x == 1 and 1 or 0
    if state.network ~= x then
        state.network = x
        if network then network(x) end
    end
end
function led.event()
    if state.event ~= 0 then return end
    sys.taskInit(function()
        state.event = 1
        for _ = 1, cfg.event.total * 2 do
            gpio.toggle(cfg.event.gpio)
            sys.wait(cfg.event.wait)
        end
        if event then event(0) end
        state.event = 0
    end)
end
return led
