import os
import time
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import traceback
import threading
from dotenv import load_dotenv
from colorama import Fore, Style, init
from APIS import scan_for_Robot
from fubon_neo.sdk import FubonSDK
from fubon_neo.constant import TimeInForce, OrderType, PriceType, MarketType, BSAction

# 初始化 colorama
init(autoreset=True)

# ===== 路徑設定 (改為當前資料夾) =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

def get_credentials():
    # 載入同資料夾下的 API.env
    env_path = os.path.join(BASE_DIR, "API.env")
    load_dotenv(dotenv_path=env_path)
    # 此處假設目前資料夾中有 F129483123.pfx，如果不一樣可以從環境變數讀取
    # os.getenv("c_path") 原本寫死網路路徑，目前替換為讀取本地檔案
    pfx_path = os.path.join(BASE_DIR, "F129483123.pfx") 
    
    return (
        os.getenv("ID"),
        os.getenv("PW"),
        pfx_path,
        os.getenv("c_pw")
    )


def print_market_data(symbol, price, volume, size):
    print(f"{Fore.BLACK}data ok :{Style.RESET_ALL} "
          f"{Fore.CYAN} N : {symbol:<6}{Style.RESET_ALL} "
          f"{Fore.YELLOW} P : {price:>7.2f}{Style.RESET_ALL} "
          f"{Fore.MAGENTA} S : {size:>4}{Style.RESET_ALL}"
          f"{Fore.WHITE} V : {volume:>8}{Style.RESET_ALL}")


def print_market_data_easy(symbol, price, volume, size):
    pass


def read_MV_df():
    # TOP300.xlsx 路徑修改 (假設放在與程式相同的資料夾)
    top300_path = os.path.join(BASE_DIR, 'TOP300.xlsx')
    if not os.path.exists(top300_path):
        print(f"⚠️ 找不到 {top300_path}，請確保此檔案放置在 {BASE_DIR} 目錄下。目前返回預設列表。")
        return []
        
    df_list = pd.read_excel(top300_path, skiprows=[0, 1, 2, 3])
    df_list_res_temp = df_list.dropna(how='any')
    df_list_res = df_list_res_temp[df_list_res_temp['股票代號'].astype(str).str.len() == 4].copy()

    df_list_res.loc[:, 'rank'] = df_list_res.iloc[:, 2].rank(ascending=False)
    df_list_res.loc[:, 'Top_OK'] = df_list_res['rank'] <= 190

    name_list = df_list_res[df_list_res['Top_OK'] == True]['股票代號'].squeeze().tolist()
    return name_list

monitor_name_list = read_MV_df()
monitor_name_list = [str(i) for i in monitor_name_list]
monitor_name_list += ['0050', '006208', '0056', '2421']


# 全域變數初始化
websocket_raw_df = {}
sss_dict = {}
subscribe_ids = monitor_name_list


# 連結 API Server
sdk = FubonSDK(300, 3)
accounts = sdk.login(*get_credentials())  # 需登入後，才能取得行情權限
print("連線帳戶資料：", accounts)
sdk.init_realtime()  # 建立行情元件連線

def handle_connect():  # 連線成功 callback
    print("行情連接成功")

def handle_disconnect(code, message):  # 連接斷線 callback
    print(f"行情連接斷線: {code}, {message}")


# 自定義函數區
def tst(time_stamp: int):
    struct_time = time.localtime(int(str(time_stamp)[0:16]))  # 轉成時間元組
    timeString = time.strftime("%H:%M:%S", struct_time)  # 轉成字串
    return timeString

def buy_sell_type(price, bid=None, ask=None):
    if ask is not None and price >= ask:
        return "B"
    elif bid is not None and price <= bid:
        return "S"
    else:
        return "--"

# Convert microseconds to datetime
def micro_to_datetime(micro):
    return datetime.fromtimestamp(micro / 1e6)

def keep_data_info(stock_num: str):
    try:
        websocket_raw_df_test = websocket_raw_df[stock_num]
        websocket_raw_df_test_df = websocket_raw_df_test[['size', 'datetime', 'b_s_type', 'price']].copy()
        return websocket_raw_df_test_df
    except Exception as e:
        print(f"Error in keep_data_info for {stock_num}: {e}")
        return False, pd.DataFrame()

def handle_message(message):
    global websocket_raw_df  # 引用全域變數
    global sss_dict

    try:
        msg = json.loads(message)
        event = msg.get("event")
        data = msg.get("data")

        if event == "pong":
            return
        if event == "subscribed":
            uid = data["id"]
            if uid in subscribe_ids:
                print(f"🚨 Error：訂閱 id {uid} 已存在列表中")
            else:
                subscribe_ids.append(uid)

        elif event == "unsubscribed":
            uid = data["id"]
            try:
                subscribe_ids.remove(uid)
            except:
                print(f"🚨 Error：查無此筆訂閱 id 資料, id {uid}")

        elif event == "data":
            symbol = data["symbol"]
            # 加入資料到對應的 symbol DataFrame
            if symbol not in websocket_raw_df:
                websocket_raw_df[symbol] = pd.DataFrame()

            # 建立表格資訊(時間+買賣別) + symbol、data整合
            data["datetime"] = micro_to_datetime(data["time"])
            data["b_s_type"] = buy_sell_type(data["price"], data.get("bid"), data.get("ask"))
            
            # 使用 pd.concat 更新 DataFrame
            websocket_raw_df[symbol] = pd.concat([websocket_raw_df[symbol], pd.DataFrame([data])], ignore_index=True)

            # 清理超過 1 分鐘的資料
            cutoff = datetime.now() - timedelta(minutes=1)
            websocket_raw_df[symbol] = websocket_raw_df[symbol][websocket_raw_df[symbol]["datetime"] >= cutoff]

            sss_dict[symbol] = keep_data_info(symbol)
            print_market_data_easy(data['symbol'], data['price'], data['volume'], data['size'])

    except Exception as e:
        handle_error(f'🚨 Error parsing JSON：{e}', traceback.format_exc())

def handle_error(error, traceback_info=None):  # 處理程式錯誤訊息 callback
    print(f'market data error: {error}')
    if traceback_info:
        print(f'Traceback:\n{traceback_info}')

stock = sdk.marketdata.websocket_client.stock
stock.on("connect", handle_connect)
stock.on("message", handle_message)
stock.on("disconnect", handle_disconnect)
stock.on("error", handle_error)

try:
    stock.disconnect()
except:
    pass
time.sleep(1)
stock.connect()


# 訂閱股票最新成交資訊
def subscribe_stocks(stock_nums: list):
    for i in stock_nums:
        stock.subscribe({
            "channel": 'trades',
            "symbol": str(i),
            "intradayOddLot": False
        })

subscribe_stocks(subscribe_ids)

# API_VOL.xlsx 路徑修改
api_vol_path = os.path.join(BASE_DIR, 'API_VOL.xlsx')
if os.path.exists(api_vol_path):
    API_VOL = pd.read_excel(api_vol_path, skiprows=[0, 1, 2, 3], index_col=0)
    API_VOL.index = API_VOL.index.astype(str)
    API_VOL['5DAY'] = API_VOL.iloc[:, -5:].mean(axis=1)
    API_VOL['10DAY'] = API_VOL.iloc[:, -10:].mean(axis=1)
else:
    print(f"⚠️ 找不到 {api_vol_path}，請確保此檔案放置在 {BASE_DIR} 目錄下。API_VOL 初始化為空。")
    API_VOL = pd.DataFrame()


# 全域 Timer 控制
scan_timer = None
scan_active = False
latest_scan_df = None
latest_scan_dict = None

def run_scan_once():
    temp_buy_dict = {}
    temp_sell_dict = {}

    res_df_buy = pd.DataFrame(columns=['Name', 'Bot_Size_30min', 'Bot_Size_EoD', '5Day_ADV', 'Ratio'])
    res_df_sell = pd.DataFrame(columns=['Name', 'Bot_Size_30min', 'Bot_Size_EoD', '5Day_ADV', 'Ratio'])
    ttt_dict = sss_dict.copy()

    for symbol in ttt_dict:
        # 如果 df 是空的或無效的，就跳過
        if not isinstance(ttt_dict[symbol], pd.DataFrame) or ttt_dict[symbol].empty:
            continue
            
        temp_buy = scan_for_Robot(df=ttt_dict[symbol], bs_type='B', cv_thres=0.39)
        temp_buy_dict[symbol] = temp_buy

        temp_sell = scan_for_Robot(df=ttt_dict[symbol], bs_type='S', cv_thres=0.39)
        temp_sell_dict[symbol] = temp_sell

        if not temp_buy.empty:
            print(f"📈  掃描處理中（{symbol}） BUY")
            buy_vol_hh = temp_buy['vol_hhour'].sum()
            buy_vol = temp_buy['vol_EoD'].sum()
            five_day = API_VOL.loc[symbol, '5DAY'] if not API_VOL.empty and symbol in API_VOL.index else 0
            ratio = buy_vol / five_day if five_day != 0 else 0
            name = API_VOL.loc[symbol, '股票名稱'] if not API_VOL.empty and symbol in API_VOL.index else 0
            res_df_buy.at[symbol, 'Bot_Size_EoD'] = buy_vol
            res_df_buy.at[symbol, 'Ratio'] = ratio
            res_df_buy.at[symbol, 'Name'] = name
            res_df_buy.at[symbol, '5Day_ADV'] = five_day
            res_df_buy.at[symbol, 'Bot_Size_30min'] = buy_vol_hh
            res_df_buy = res_df_buy.sort_values(by='Ratio', ascending=False)

        if not temp_sell.empty:
            print(f"📉  掃描處理中（{symbol}） SELL")
            sell_vol_hh = temp_sell['vol_hhour'].sum()
            sell_vol = temp_sell['vol_EoD'].sum()
            five_day = API_VOL.loc[symbol, '5DAY'] if not API_VOL.empty and symbol in API_VOL.index else 0
            ratio = sell_vol / five_day if five_day != 0 else 0
            name = API_VOL.loc[symbol, '股票名稱'] if not API_VOL.empty and symbol in API_VOL.index else 0
            res_df_sell.at[symbol, 'Bot_Size_EoD'] = sell_vol
            res_df_sell.at[symbol, 'Ratio'] = ratio
            res_df_sell.at[symbol, 'Name'] = name
            res_df_sell.at[symbol, '5Day_ADV'] = five_day
            res_df_sell.at[symbol, 'Bot_Size_30min'] = sell_vol_hh
            res_df_sell = res_df_sell.sort_values(by='Ratio', ascending=False)

    print("✅  掃描完成！BUY 筆數：", len(res_df_buy), "SELL 筆數：", len(res_df_sell))
    print("🕒 掃描時間戳記：", datetime.now().strftime("%H:%M:%S"))

    return [[res_df_buy, res_df_sell], [temp_buy_dict, temp_sell_dict]]


def print_blmsg(latest_scan_df):
    print("")
    if latest_scan_df is None or not isinstance(latest_scan_df, (list, tuple)) or len(latest_scan_df) < 2:
        print("❗ latest_scan_df 尚未初始化或格式錯誤")
        return

    print(" ** Intra-day TWAP Bot Detector ** ")
    print("Buy：")
    if not isinstance(latest_scan_df[0], pd.DataFrame):
        print("Object Empty")
    else:
        buy_df = latest_scan_df[0][latest_scan_df[0]["Ratio"] >= 0.1]
        if len(buy_df) > 0:
            print(buy_df.iloc[0:3, :].to_string(formatters={'Ratio': '{:.2f}'.format}))
            print("")
        else:
            print("No buy robot...")

    print("SELL：")
    if not isinstance(latest_scan_df[1], pd.DataFrame):
        print("Object Empty")
    else:
        sell_df = latest_scan_df[1][latest_scan_df[1]["Ratio"] >= 0.1]
        if len(sell_df) > 0:
            print(sell_df.iloc[0:3, :].to_string(formatters={'Ratio': '{:.2f}'.format}))
        else:
            print("No sell robot...")


def periodic_scan():
    global scan_timer, latest_scan_df, latest_scan_dict, scan_active, agg_df

    if not scan_active:
        print("⏹️ 掃描任務已停止，不再執行")
        return

    now = datetime.now()
    scan_start_time = now.replace(hour=9, minute=1, second=0, microsecond=0)
    cutoff_time = now.replace(hour=13, minute=25, second=0, microsecond=0)

    if now >= cutoff_time or now <= scan_start_time:
        scan_active = False
        print("⏹️ 非掃描時間，自動停止掃描任務")
        return

    # 執行掃描
    res = run_scan_once()
    latest_scan_df = res[0]
    latest_scan_dict = res[1]
    print_blmsg(latest_scan_df)

    # 排程下一次
    if scan_active:
        scan_timer = threading.Timer(60, periodic_scan)
        scan_timer.start()

    return [latest_scan_df, latest_scan_dict]

agg_df = pd.DataFrame()

def start_scan():
    global scan_timer, scan_active, agg_df
    if scan_timer is None or not scan_timer.is_alive():
        scan_active = True
        print("✅ 啟動掃描任務")
        scan_timer = threading.Timer(0, periodic_scan)
        scan_timer.start()
    else:
        print("⚠️ 掃描任務已在執行中")

def stop_scan():
    global scan_timer, scan_active
    scan_active = False
    if scan_timer is not None:
        scan_timer.cancel()
        print("⏸️ 已暫停掃描任務")


# 使用者選擇
if __name__ == "__main__":
    try:
        choice = input('A > START, B > END...').strip().upper()
        if choice == "A":
            print("✅ 準備掃描任務")
            start_scan()
        elif choice == "B":
            print("⏸️ 準備暫停掃描任務")
            stop_scan()
        else:
            print('🦽 You are not alone...')
    except KeyboardInterrupt:
        print("\n程式中止。")
        stop_scan()
        try:
            stock.disconnect()
        except:
            pass
