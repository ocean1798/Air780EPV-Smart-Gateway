_G.GATEWAY_VERSION = "1.2.4"
return {
    network = {
        dns = {
            '180.184.2.2',
            '223.5.5.5'
        },
        IPv6 = 0
    },
    notify = {
        feishu = {
            enable = 1,
            url = 'https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_FEISHU_BOT_TOKEN'
        },
        wecom = {
            enable = 0,
            url = ''
        },
        dingtalk = {
            enable = 0,
            url = ''
        },
        bark = {
            enable = 0,
            url = '',  -- 填写 Bark API URL，如 'https://api.day.app/your_key/'
            group = 'Air780EPV',
            sound = 'minuet'
        },
        webhook = {
            enable = 0,
            url = ''
        }
    },
    serial = {
        id = uart.VUART_0,
        baud = 115200
    },
    rndis = {
        default_enable = false
    },
    cellular_data = {
        default_enable = false
    },
    system = {
        daily_reboot_hour = -1
    },
    fota = {
        version_url = "http://your-bucket-domain.clouddn.com/version.json",
        default_bin_url = "http://your-bucket-domain.clouddn.com/script.bin",
        trigger_callers = {
            "13800138000",
            "*"
        }
    }
}
