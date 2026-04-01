from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.on("request", lambda r: print(">>", r.method, r.url, r.post_data) if "api" in r.url else None)
    page.goto("https://mis.taifex.com.tw/futures/VolatilityQuotes/")
    page.wait_for_timeout(2000)
    
    # Click "接受"
    page.locator("button:has-text('接受')").first.click()
    page.wait_for_timeout(3000)
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
