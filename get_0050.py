import pandas as pd

def get_0050_components():
    """
    爬取最新的 0050 (元大台灣50) 成分股名單與持股(千股)比例。
    資料來源：MoneyDJ
    """
    url = 'https://www.moneydj.com/ETF/X/Basic/Basic0007A.xdjhtm?etfid=0050.TW'
    try:
        # 讀取網頁表格
        dfs = pd.read_html(url)
        
        results = []
        for df in dfs:
            # 尋找包含成分股資訊的確切表格
            if '股票名稱' in df.columns and '持股(千股)' in df.columns:
                for _, row in df.iterrows():
                    name = str(row['股票名稱']).strip()
                    # 排除掉不合理的一般說明文字或空值
                    if name != 'nan' and pd.notna(row['持股(千股)']):
                        results.append({
                            '股票名稱': name,
                            '持股(千股)': float(row['持股(千股)']),
                            '比例(%)': float(row['比例']) if '比例' in row else 0.0,
                            '增減': str(row['增減']).strip() if '增減' in row else ''
                        })
        
        res_df = pd.DataFrame(results)
        
        if not res_df.empty:
            # 清理與排序
            res_df = res_df.drop_duplicates(subset=['股票名稱'])
            res_df = res_df.sort_values(by='比例(%)', ascending=False).reset_index(drop=True)
            
        return res_df
        
    except Exception as e:
        print(f"❌ 爬取 0050 成分股時發生錯誤: {e}")
        return pd.DataFrame()

if __name__ == "__main__":
    print("⏳ 正在即時抓取最新 0050 成分股清單與權重...")
    df = get_0050_components()
    
    if not df.empty:
        csv_path = "0050_components.csv"
        # 儲存為 CSV（使用 utf-8-sig 讓 Excel 開啟不亂碼）
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        
        print(f"✅ 成功抓取 {len(df)} 檔成分股！已自動儲存至 {csv_path}")
        print("-" * 40)
        print("🏆 前 10 大成分股預覽：")
        print(df.head(10).to_string(index=False))
        print("-" * 40)
    else:
        print("⚠️ 無法獲取成分股，請稍後再試。")
