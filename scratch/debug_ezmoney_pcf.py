"""
ezmoney PCF 抓取診斷工具。

用途：主動式 ETF (00981A / 00403A) 的淨值來源只有 ezmoney 這一頁，
一旦網站改版或 fundCode 換掉，main.py 的爬蟲會靜默失敗並改吃過期的本地 JSON。
這支腳本會把「頁面上有哪些基金、fundCode 是什麼、GetPCF 有沒有回應、
回來的是不是你要的那一檔」全部印出來。

用法（建議用終端機跑）：
    cd ~/Desktop/Github/order_book/api_broker
    python3 scratch/debug_ezmoney_pcf.py            # 無頭模式
    python3 scratch/debug_ezmoney_pcf.py --head     # 開視窗，可以自己看頁面長怎樣

在 Spyder / Jupyter 裡跑也可以（會自己開執行緒，避開既有的 event loop）：
    from debug_ezmoney_pcf import run
    run()          # 或 run(headless=False)

把輸出整段貼回來就能判斷是哪一段壞掉。
"""
import asyncio
import json
import re
import sys

try:
    from playwright.async_api import async_playwright  # noqa: F401
except ImportError:
    sys.exit("找不到 playwright。請先安裝：\n"
             "    pip install playwright && python3 -m playwright install chromium\n"
             "（要用跟 main.py 同一個 Python 環境）")

PCF_URL = "https://www.ezmoney.com.tw/ETF/Transaction/PCF"
TARGETS = ["00981A", "00403A"]
KNOWN_CODES = {"00981A": "49YTW", "00403A": "63YTW"}

LIST_OPTIONS_JS = """() => {
    const out = [];
    document.querySelectorAll('select').forEach((s, si) => {
        s.querySelectorAll('option').forEach(o => out.push({
            kind: 'option',
            select: s.id || s.name || ('select#' + si),
            value: (o.value || '').trim(),
            text: (o.textContent || '').trim()
        }));
    });
    document.querySelectorAll('a[href*="fundCode"], [data-fundcode], [data-fund-code]').forEach(a => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/fundCode=([^&#]+)/i);
        out.push({
            kind: 'link',
            select: href,
            value: (a.getAttribute('data-fundcode') || a.getAttribute('data-fund-code') || (m ? m[1] : '')).trim(),
            text: (a.textContent || '').trim()
        });
    });
    return out;
}"""


def summarise(data, indent="    "):
    """把 GetPCF 的回應內容摘要出來。"""
    print(f"{indent}top-level keys: {list(data.keys())}")

    fund = data.get("fund") or {}
    if isinstance(fund, dict) and fund:
        print(f"{indent}fund.sStockNo   = {fund.get('sStockNo')!r}")
        print(f"{indent}fund.sFundName  = {fund.get('sFundName')!r}")
        print(f"{indent}fund.FundDate   = {fund.get('FundDate')!r}")

    pcf_list = data.get("pcf") or []
    pcf_items = {i.get("PCFCode"): i.get("Amount")
                 for i in pcf_list if isinstance(i, dict)}
    print(f"{indent}pcf 欄位: {list(pcf_items.keys())}")
    for k in ("NAV", "OUT_UNIT", "P_UNIT", "FUND_BASEUNIT", "DIFF_ACT_AMT"):
        print(f"{indent}  {k:15} = {pcf_items.get(k)}")
    try:
        nav_total = float(pcf_items.get("NAV") or 0)
        out_unit = float(pcf_items.get("OUT_UNIT") or 0)
        if nav_total and out_unit:
            print(f"{indent}  每單位淨值      = {nav_total / out_unit:.6f} "
                  f" (P_UNIT={pcf_items.get('P_UNIT')})")
    except (TypeError, ValueError):
        pass

    if pcf_list and isinstance(pcf_list[0], dict):
        print(f"{indent}TranDate = {pcf_list[0].get('TranDate')!r}  "
              f"PostDate = {pcf_list[0].get('PostDate')!r}")

    groups = [g.get("AssetCode") for g in (data.get("asset") or []) if isinstance(g, dict)]
    n_assets = sum(len(g.get("Details") or [])
                   for g in (data.get("asset") or []) if isinstance(g, dict))
    weight = sum(float(d.get("NavRate") or 0)
                 for g in (data.get("asset") or []) if isinstance(g, dict)
                 for d in (g.get("Details") or []) if isinstance(d, dict))
    print(f"{indent}asset 群組 = {groups}，明細共 {n_assets} 筆")
    print(f"{indent}NavRate 合計 = {weight:.2f}%   （100 − 這個數 = 現金/其他資產比例）")


async def main(headless=True):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        # 每筆回應各自 append 一筆完整紀錄，不共用可變狀態，
        # 避免「await resp.json() 期間主流程把 dict 清掉」的競態。
        records = []
        state = {"gen": 0}

        async def on_response(resp):
            gen = state["gen"]          # 回應「抵達當下」的階段編號
            url = resp.url
            low = url.lower()
            is_pcf = "getpcf" in low
            if not (is_pcf or any(k in low for k in ("pcf", "fund", "asset", "nav", "/api/"))):
                return
            payload = None
            if is_pcf:
                try:
                    payload = await resp.json()
                except Exception as e:  # noqa: BLE001
                    payload = {"__parse_error__": repr(e)}
            records.append({
                "gen": gen,
                "url": url,
                "status": resp.status,
                "is_pcf": is_pcf,
                "json": payload,
            })

        page.on("response", on_response)

        # 順便把 GetPCF 的「請求」長相記下來 —— 如果它只是一個帶 fundCode 的
        # 普通 POST/GET，就能把整套 Playwright 換成一行 requests，穩定度差很多。
        requests_seen = []

        def on_request(req):
            if "getpcf" in req.url.lower():
                try:
                    body = req.post_data
                except Exception:  # noqa: BLE001
                    body = "<unavailable>"
                requests_seen.append({
                    "gen": state["gen"],
                    "method": req.method,
                    "url": req.url,
                    "post_data": body,
                    "content_type": req.headers.get("content-type"),
                    "x_requested_with": req.headers.get("x-requested-with"),
                    "referer": req.headers.get("referer"),
                })

        page.on("request", on_request)

        async def wait_for_pcf(gen, seconds=12):
            for _ in range(int(seconds * 2)):
                hits = [r for r in records
                        if r["gen"] == gen and r["is_pcf"] and isinstance(r["json"], dict)
                        and "__parse_error__" not in r["json"]]
                if hits:
                    return hits[-1]
                await asyncio.sleep(0.5)
            return None

        print("=" * 72)
        print("STEP 1 — 開啟 PCF 清單頁（不帶 fundCode）")
        print("=" * 72)
        state["gen"] = 1
        try:
            await page.goto(PCF_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001
            print(f"!! 連線失敗: {e}")
            await browser.close()
            return
        try:
            await page.wait_for_selector('select option, a[href*="fundCode"]', timeout=10000)
        except Exception:  # noqa: BLE001
            print("!! 10 秒內找不到 select/連結，頁面結構可能變了")

        print(f"標題: {await page.title()!r}")
        print(f"最終網址: {page.url}")

        options = await page.evaluate(LIST_OPTIONS_JS)
        discovered = {}
        for o in options:
            if not o["value"]:
                continue
            m = re.search(r"(\d{4,6}[A-Z]?)", o["text"])
            if m:
                discovered.setdefault(m.group(1), o["value"])

        print(f"\n解析出的 代號 -> fundCode 對照（共 {len(discovered)} 檔）：")
        for k, v in sorted(discovered.items()):
            mark = ""
            if k in KNOWN_CODES:
                mark = ("  <-- 與程式內建相同" if KNOWN_CODES[k] == v
                        else f"  <<< 不一樣！程式內建是 {KNOWN_CODES[k]}")
            print(f"  {k} -> {v}{mark}")
        for t in TARGETS:
            if t not in discovered:
                print(f"  !! {t} 不在選單裡")

        base_hit = await wait_for_pcf(1, seconds=3)
        print(f"\n清單頁本身有沒有觸發 GetPCF: {'有' if base_hit else '沒有'}")
        if base_hit:
            print(f"  {base_hit['status']} {base_hit['url']}")
            print("  （這是預設帶出來的那一檔，內容如下，用來確認 fundCode 到底有沒有生效）")
            summarise(base_hit["json"], indent="    ")

        for step, ticker in enumerate(TARGETS, start=2):
            code = discovered.get(ticker) or KNOWN_CODES.get(ticker)
            print()
            print("=" * 72)
            print(f"STEP {step} — 抓 {ticker} (fundCode={code})")
            print("=" * 72)
            if not code:
                print("!! 沒有可用的 fundCode，跳過")
                continue

            # --- 方式 A：直接用 query string 開頁 ---
            state["gen"] = step * 10
            print(f"[A] 直接開 {PCF_URL}?fundCode={code}")
            try:
                await page.goto(f"{PCF_URL}?fundCode={code}",
                                wait_until="domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                print(f"    導頁失敗: {e}")
            hit_a = await wait_for_pcf(state["gen"])
            gen_a = state["gen"]
            if hit_a:
                print(f"    >>> 有回應 {hit_a['status']} {hit_a['url']}")
                summarise(hit_a["json"], indent="        ")
            else:
                seen = [f"{r['status']} {r['url']}" for r in records if r["gen"] == gen_a]
                print(f"    >>> 沒有 GetPCF。同期攔到 {len(seen)} 筆相關請求：")
                for u in seen[:10]:
                    print("        " + u)

            # --- 方式 B：在下拉選單上選取 ---
            state["gen"] = step * 10 + 1
            print(f"[B] 用下拉選單選 {code}")
            try:
                await page.goto(PCF_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_selector(f'select option[value="{code}"]', timeout=10000)
                sel = await page.query_selector(f'select:has(option[value="{code}"])')
                if sel:
                    await sel.select_option(code)
                else:
                    print("    找不到含這個 option 的 select")
            except Exception as e:  # noqa: BLE001
                print(f"    選取失敗: {e}")
            hit_b = await wait_for_pcf(state["gen"])
            gen_b = state["gen"]
            if hit_b:
                print(f"    >>> 有回應 {hit_b['status']} {hit_b['url']}")
                summarise(hit_b["json"], indent="        ")
            else:
                seen = [f"{r['status']} {r['url']}" for r in records if r["gen"] == gen_b]
                print(f"    >>> 沒有 GetPCF。同期攔到 {len(seen)} 筆相關請求：")
                for u in seen[:10]:
                    print("        " + u)

            best = hit_a or hit_b
            if best:
                got = str(((best["json"].get("fund") or {}).get("sStockNo") or "")).strip()
                if got and got.upper() != ticker.upper():
                    print(f"    !!!! 回來的是 {got}，不是 {ticker} —— fundCode 沒生效，"
                          f"main.py 會拿到別檔基金的淨值")
                out = f"/tmp/ezmoney_{ticker}_raw.json"
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(best["json"], f, ensure_ascii=False, indent=2)
                print(f"    原始回應已存到 {out}")

        print()
        print("=" * 72)
        print("STEP 9 — GetPCF 的請求長相（判斷能不能拋棄 Playwright 改用 requests）")
        print("=" * 72)
        if not requests_seen:
            print("沒攔到任何 GetPCF 請求")
        for r in requests_seen:
            print(f"  [gen={r['gen']}] {r['method']} {r['url']}")
            print(f"      Content-Type     : {r['content_type']}")
            print(f"      X-Requested-With : {r['x_requested_with']}")
            print(f"      Referer          : {r['referer']}")
            print(f"      Body             : {r['post_data']!r}")

        # cookie 也一起看，判斷 GetPCF 是不是靠 session 記住選了哪一檔
        try:
            cookies = await page.context.cookies()
            names = sorted({c.get("name") for c in cookies})
            print(f"\n  目前 cookie（{len(cookies)} 個）: {names}")
        except Exception as e:  # noqa: BLE001
            print(f"\n  取 cookie 失敗: {e}")

        await browser.close()


def run(headless=True):
    """在自己的執行緒裡跑，這樣在 Spyder / Jupyter（已經有 event loop）也能用。

    直接在 IPython 主控台呼叫也可以：
        from debug_ezmoney_pcf import run; run()
    """
    import threading

    box = {}

    def target():
        try:
            asyncio.run(main(headless=headless))
        except BaseException as e:      # noqa: BLE001 - 要把例外帶回主執行緒
            box["error"] = e

    t = threading.Thread(target=target, name="ezmoney-pcf-debug")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]


if __name__ == "__main__":
    run(headless="--head" not in sys.argv)
