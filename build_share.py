"""
build_share.py
================
產出可離線分享的單一 HTML 檔。

執行流程:
  1. 跑 disposal_checker.fetch_disposed_stocks() 取得處置中清單
  2. 跑 disposal_checker.check_all_conditions() 取得全條款掃描結果
  3. 對「處置中 + 高風險」全部股票呼叫 check_single_stock(),收集個股詳細資料
  4. 讀 static/disposal.html 作為模板,在 </head> 之前插入一段:
        <script>window.__SHARE_DATA__ = { ... };</script>
  5. 寫出 disposal_share_<日期>.html(同層目錄,單一檔案)

接收者:雙擊 .html 即可,不需要任何後端或網路。
"""

from __future__ import annotations

import os
import sys
import json
import datetime
import argparse


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "static", "disposal.html")


def _safe_call(label, fn, *args, **kwargs):
    """執行 fn 並把例外轉成 {"error": ...} payload,避免一個錯就整個爛掉。"""
    try:
        print(f"  → {label} ...")
        result = fn(*args, **kwargs)
        return result
    except Exception as e:
        print(f"  ✗ {label} 失敗: {e}")
        return {"error": f"{label} 失敗: {e}"}


def main():
    parser = argparse.ArgumentParser(description="產出可離線分享的 disposal HTML")
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="輸出檔名(預設 disposal_share_YYYYMMDD_HHMM.html,放在腳本同層)",
    )
    parser.add_argument(
        "--skip-scan",
        action="store_true",
        help="跳過全條款掃描(只含處置中清單,產出較快、檔案較小)",
    )
    args = parser.parse_args()

    # 確保 working dir 是 BASE_DIR,讓 disposal_checker 載入 .env 與快取一致
    os.chdir(BASE_DIR)
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    print("=" * 60)
    print("離線分享版 disposal HTML 產生器")
    print("=" * 60)

    # 1. 處置中清單
    print("\n[1/3] 取得處置中清單...")
    from disposal_checker import fetch_disposed_stocks
    disposed_payload = _safe_call("fetch_disposed_stocks", fetch_disposed_stocks)
    disposed_dict = (disposed_payload or {}).get("disposed", {}) if isinstance(disposed_payload, dict) else {}
    print(f"  ✓ 處置中: {len(disposed_dict)} 檔")

    # 2. 全條款掃描
    scan_payload = None
    if args.skip_scan:
        print("\n[2/3] 跳過全條款掃描 (--skip-scan)")
        scan_payload = {"error": "本份分享檔未包含掃描資料"}
    else:
        print("\n[2/3] 執行全條款掃描 (首次需 15-30 秒)...")
        from disposal_checker import check_all_conditions
        scan_payload = _safe_call("check_all_conditions", check_all_conditions)
        if isinstance(scan_payload, dict) and "stocks" in scan_payload:
            print(f"  ✓ 高風險: {scan_payload.get('total_flagged', 0)} 檔")

    # 3. 蒐集需要附帶個股詳細資料的代號 = 處置中 ∪ 高風險
    print("\n[3/3] 收集個股詳細資料...")
    sid_set = set(disposed_dict.keys())
    if isinstance(scan_payload, dict):
        for s in scan_payload.get("stocks", []) or []:
            sid = s.get("stock_id")
            if sid:
                sid_set.add(sid)

    stock_reports: dict = {}
    if sid_set:
        from disposal_checker import check_single_stock
        # 排序穩定一點
        for i, sid in enumerate(sorted(sid_set), 1):
            try:
                rep = check_single_stock(sid)
                if rep is not None:
                    stock_reports[sid] = rep
            except Exception as e:
                print(f"  ✗ {sid} 失敗: {e}")
            if i % 25 == 0:
                print(f"  ... 已處理 {i}/{len(sid_set)}")
        print(f"  ✓ 個股報表: {len(stock_reports)} 檔")
    else:
        print("  (無股票需要個股資料)")

    # 4. 組 SHARE_DATA payload
    now = datetime.datetime.now()
    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "disposed_payload": disposed_payload,
        "scan_payload": scan_payload,
        "stock_reports": stock_reports,
    }

    # 5. 讀模板,注入 payload
    print("\n[輸出] 組合 HTML...")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    # 用 json.dumps 確保 payload 安全嵌入。
    # ensure_ascii=False 保留中文可讀;separators 壓掉空白省檔案大小。
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    # 防 </script> 截斷:把字串中的 "</" 換掉
    payload_json = payload_json.replace("</", "<\\/")

    inject = (
        "<script>\n"
        "// === 離線分享資料(由 build_share.py 注入) ===\n"
        f"window.__SHARE_DATA__ = {payload_json};\n"
        "</script>\n"
    )

    # 在 </head> 之前插入;若找不到 </head> 就退而附加到 <body> 開頭
    if "</head>" in template:
        out_html = template.replace("</head>", inject + "</head>", 1)
    else:
        out_html = inject + template

    # 輸出
    out_name = args.output or f"disposal_share_{now.strftime('%Y%m%d_%H%M')}.html"
    out_path = out_name if os.path.isabs(out_name) else os.path.join(BASE_DIR, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out_html)

    size_kb = os.path.getsize(out_path) / 1024.0
    print(f"\n✅ 完成: {out_path}")
    print(f"   檔案大小: {size_kb:.1f} KB")
    print(f"   產出時間: {payload['generated_at']}")
    print("\n接收者只要雙擊這個 .html,瀏覽器就會打開,無需任何後端或網路。")


if __name__ == "__main__":
    main()
