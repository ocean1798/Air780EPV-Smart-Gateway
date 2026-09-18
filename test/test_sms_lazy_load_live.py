# -*- coding: utf-8 -*-
"""
Air780EPV 智能网关 - 真实实机短信收件箱触底自动懒加载与游标分页端到端验证
通过 Playwright 自动化验证：
1. 初始首屏仅拉取 15 条短信（防板端 OOM）；
2. 容器触底自动触发 IntersectionObserver（或点击）拉取第 2 批（15 -> 30 条）；
3. 再次触底拉取第 3 批（30 -> 36 条），游标闭环并显示“✓ 已加载全部 36 条脱机记录”；
4. 截取桌面端与移动端实测画面归档至 specs 目录。
"""

import os
import sys
import time
import json
import threading
from playwright.sync_api import sync_playwright

# 注入当前网关模块路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST_GATEWAY_DIR = os.path.join(BASE_DIR, "tools", "host_gateway")
sys.path.insert(0, HOST_GATEWAY_DIR)

from gateway_hub import GatewayHub
from gateway_web import WebServer, DEFAULT_WEB_HOST, DEFAULT_WEB_PORT

SPECS_DIR = os.path.join(BASE_DIR, "specs")
os.makedirs(SPECS_DIR, exist_ok=True)

def run_live_lazy_load_test():
    print("=== 启动 Air780EPV 物理中枢与 Web 控制台 ===")
    hub = GatewayHub()
    hub_thread = threading.Thread(target=hub.start, daemon=True)
    hub_thread.start()
    time.sleep(1.0)

    web = WebServer(host=DEFAULT_WEB_HOST, port=DEFAULT_WEB_PORT)
    web_thread = threading.Thread(target=web.start, daemon=True)
    web_thread.start()
    time.sleep(1.0)

    url = f"http://127.0.0.1:{DEFAULT_WEB_PORT}"
    print(f"Web 控制台已就绪: {url}")

    success = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # 1. 桌面视口测试 (1280x820)
            context = browser.new_context(viewport={"width": 1280, "height": 820})
            page = context.new_page()
            print(f"正在打开控制台: {url}")
            page.goto(url)
            page.wait_for_load_state("domcontentloaded")

            # 等待首屏短信卡片渲染
            page.wait_for_selector(".sms-item", timeout=8000)
            time.sleep(1.0)

            cards = page.query_selector_all(".sms-item")
            count_p1 = len(cards)
            print(f"[阶段 1] 首屏渲染完成，当前短信卡片数量: {count_p1} 条 (预期 15)")
            assert count_p1 == 15, f"首屏卡片数量不符预期: {count_p1} != 15"

            load_more = page.query_selector(".sms-load-more")
            assert load_more is not None, "未找到加载更多容器 .sms-load-more"
            txt = load_more.inner_text().strip()
            print(f"  底部加载状态提示: {txt}")

            p1_img = os.path.join(SPECS_DIR, "lazy_load_p1_desktop.png")
            page.screenshot(path=p1_img)
            print(f"  首屏证据截图已保存: {p1_img}")

            # 模拟向下滚动或直接触发加载更多
            print("\n[阶段 2] 触发向下滚动拉取第二批...")
            page.evaluate("""() => {
                const container = document.getElementById('smsContainer');
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            }""")
            # 如果 IntersectionObserver 响应，等待条数变更为 30；如果几秒没变则触发点击兜底
            t0 = time.time()
            loaded_p2 = False
            while time.time() - t0 < 5.0:
                cards = page.query_selector_all(".sms-item")
                if len(cards) >= 30:
                    loaded_p2 = True
                    break
                # 点击一次 load-more
                lm = page.query_selector(".sms-load-more:not(.sms-loaded-all)")
                if lm:
                    try:
                        lm.click()
                    except Exception:
                        pass
                time.sleep(0.5)

            count_p2 = len(page.query_selector_all(".sms-item"))
            print(f"  第二批加载完成，当前短信卡片数量: {count_p2} 条 (预期 30)")
            assert count_p2 == 30, f"第二批数量不符预期: {count_p2} != 30"

            p2_img = os.path.join(SPECS_DIR, "lazy_load_p2_desktop.png")
            page.screenshot(path=p2_img)
            print(f"  第二批证据截图已保存: {p2_img}")

            # 再次向下滚动拉取剩余全部记录
            print("\n[阶段 3] 触发向下滚动拉取第三批 (最终剩余记录)...")
            page.evaluate("""() => {
                const container = document.getElementById('smsContainer');
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            }""")
            t0 = time.time()
            loaded_all = False
            while time.time() - t0 < 5.0:
                cards = page.query_selector_all(".sms-item")
                if len(cards) >= 36:
                    loaded_all = True
                    break
                lm = page.query_selector(".sms-load-more:not(.sms-loaded-all)")
                if lm:
                    try:
                        lm.click()
                    except Exception:
                        pass
                time.sleep(0.5)

            count_all = len(page.query_selector_all(".sms-item"))
            print(f"  全部加载完成，当前短信卡片数量: {count_all} 条 (预期 36)")
            assert count_all == 36, f"全部卡片数量不符预期: {count_all} != 36"

            end_all = page.query_selector(".sms-loaded-all")
            assert end_all is not None, "未找到已加载全部记录提示 .sms-loaded-all"
            end_txt = end_all.inner_text().strip()
            print(f"  全部加载提示文本: {end_txt}")
            assert "36" in end_txt, f"提示文本中未包含总数 36: {end_txt}"

            all_img = os.path.join(SPECS_DIR, "lazy_load_all_desktop.png")
            page.screenshot(path=all_img)
            print(f"  全部加载证据截图已保存: {all_img}")

            # 4. 移动端视角截屏 (360x812)
            print("\n[阶段 4] 验证移动端响应式视图...")
            page.set_viewport_size({"width": 360, "height": 812})
            time.sleep(0.5)
            page.evaluate("""() => {
                const container = document.getElementById('sms-container');
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            }""")
            time.sleep(0.5)
            mobile_img = os.path.join(SPECS_DIR, "lazy_load_mobile_360x812.png")
            page.screenshot(path=mobile_img)
            print(f"  移动端证据截图已保存: {mobile_img}")

            browser.close()
            success = True
            print("\n🎉 实机懒加载全部测试 100% 验证通过！")
    finally:
        print("正在停止 Web 与 Hub 服务...")
        web.stop()
        hub.stop()

    return success

if __name__ == "__main__":
    ok = run_live_lazy_load_test()
    sys.exit(0 if ok else 1)
