"""
注意有價證券 - 全條款異常偵測（第1~13款）
===========================================
依據：臺灣證券交易所/櫃買中心
      公布或通知注意交易資訊暨處置作業要點
      第四條第一項各款異常標準

【略過條款】
  第5款：需個別券商交易資料（FinLab未提供），略過。
  第8款：臺灣存託憑證（TDR）專用，略過。
"""

import os
import ssl
import pandas as pd
import numpy as np
import requests
import urllib3
import traceback
from datetime import datetime, date as date_type

# --- 全局忽略 SSL 驗證 (解決 macOS 憑證未更新導致的連線錯誤) ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1. 覆寫 requests (FinLab 與 TWSE/TPEx 使用)
original_request = requests.Session.request
def patched_request(self, method, url, **kwargs):
    kwargs['verify'] = False
    return original_request(self, method, url, **kwargs)
requests.Session.request = patched_request

# 2. 覆寫底層 urllib (若其他套件依賴)
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass
# -----------------------------------------------------------

# ============================================================
# FinLab 資料欄位設定（請依實際 API 調整）
# ============================================================
F_CLOSE        = 'price:收盤價'
F_VOLUME       = 'price:成交股數'              # 成交量（股）
F_AMOUNT       = 'price:成交金額'              # 成交金額（元）
F_COMPANY      = 'company_basic_info'          # 公司基本資料
F_PE           = 'price_earning_ratio:本益比'
F_PBR          = 'price_earning_ratio:股價淨值比'
F_MG_BUY       = 'margin_transactions:融資今日餘額'
F_MG_SELL      = 'margin_transactions:融券今日餘額'
F_MG_BUY_RATE  = 'margin_transactions:融資使用率'
F_MG_SELL_RATE = 'margin_transactions:融券使用率'
F_LEND         = 'security_lending_sell:借券賣出'
F_DAYTRADE     = 'intraday_trading:當日沖銷交易成交股數'

# ============================================================
# 門檻常數（對應法規原文）
# ============================================================
MIN_PRICE      = 5.0      # 除外：收盤未滿 NT$5
MIN_CAPITAL    = 8e7      # 除外：實收資本額未達 8,000萬
MIN_IND_PEERS  = 5        # 同類股至少需達此家數

# 第1款
C1_A_RET      = 30.0;  C1_B_RET     = 23.0
C1_DIFF       = 20.0;  C1_PRICE_GAP = 40.0

# 第2款
C2_D30        = 100.0; C2_D30_LOW   = 120.0
C2_D60        = 140.0; C2_D60_LOW   = 180.0
C2_D90        = 160.0; C2_D90_LOW   = 240.0
C2_DIFF       = 80.0

# 第3款
C3_RET        = 27.0;  C3_DIFF      = 20.0
C3_VOL_X      = 5.0;   C3_VOL_DIFF  = 4.0
C3_MIN_TURN   = 1.0;   C3_MIN_VOL   = 300

# 第4款
C4_RET        = 27.0;  C4_DIFF      = 20.0
C4_TURN       = 5.0;   C4_TURN_DIFF = 3.0

# 第6款
C6_PE         = 65.0;  C6_PE_X      = 2.0
C6_PBR        = 4.0;   C6_PBR_X     = 2.0
C6_TURN       = 5.0;   C6_VOL       = 2000
C6_PBR_IND_X  = 2.0

# 第7款
C7_RET        = 27.0;  C7_DIFF      = 20.0
C7_MR         = 10.0;  C7_MBU       = 20.0
C7_MSE        = 10.0;  C7_MR_EXP    = 4.0

# 第9款
C9_X          = 5.0;   C9_DIFF      = 4.0
C9_MIN_TURN   = 1.0;   C9_MIN_VOL   = 300
C9_MIN_AMT    = 2e7

# 第10款
C10_TURN6     = 80.0;  C10_TURN6_DIFF = 50.0
C10_TURN      = 5.0;   C10_TURN_DIFF  = 3.0
C10_MIN_AMT   = 2e8

# 第11款
C11_GAP       = 70.0;  C11_STEP     = 300.0
C11_STEP_ADD  = 15.0

# 第12款
C12_RATIO     = 9.0;   C12_X        = 4.0
C12_MIN_TURN  = 0.3;   C12_MIN_VOL  = 500
C12_MIN_LEND  = 100

# 第13款
C13_RATIO     = 60.0;  C13_MIN_TURN = 5.0
C13_MIN_AMT   = 2e8;   C13_MIN_DT   = 2000


# ============================================================
# 初始化 / 快取
# ============================================================
_finlab_initialized = False
_CACHE: dict = {}
_scan_response = None
_scan_date = None
_condition_series = None   # {1: pd.Series, 2: pd.Series, ...}
_loaded_data = None        # {"close": df, "volume": df, ...}


def init_finlab():
    """初始化 FinLab，使用環境變數中的 Token。"""
    global _finlab_initialized
    if _finlab_initialized:
        return
    import finlab
    token = os.getenv("FINLAB_TOKEN", "")
    if not token:
        raise ValueError("FINLAB_TOKEN 環境變數未設定，請在 API.env 中加入")
    finlab.login(api_token=token)
    _finlab_initialized = True
    print("✅ FinLab 登入成功")


def load(field: str):
    """載入並快取 FinLab 資料；失敗回傳 None。"""
    if field not in _CACHE:
        try:
            from finlab import data as finlab_data
            _CACHE[field] = finlab_data.get(field)
        except Exception as e:
            print(f"⚠️  [{field}] 無法取得，相關條款自動跳過：{e}")
            _CACHE[field] = None
    return _CACHE[field]


def clear_cache():
    """清除快取，強制重新載入。"""
    global _scan_response, _scan_date, _condition_series, _loaded_data
    _CACHE.clear()
    _scan_response = None
    _scan_date = None
    _condition_series = None
    _loaded_data = None


# ============================================================
# 工具函式
# ============================================================
def ind_avg_vec(ret: pd.Series, cat: pd.Series) -> pd.Series:
    """向量化計算各股同產業平均漲跌幅（排除自身）。"""
    df = pd.DataFrame({'r': ret, 'c': cat}).dropna(subset=['c'])
    if df.empty:
        return pd.Series(np.nan, index=ret.index)
    grp_s = df.groupby('c')['r'].sum()
    grp_n = df.groupby('c')['r'].count()
    ms = df['c'].map(grp_s)
    mn = df['c'].map(grp_n)
    avg = (ms - df['r']) / (mn - 1)
    avg[mn < MIN_IND_PEERS] = np.nan
    return avg.reindex(ret.index)


def all_diff_ok(ret, avg_all, thr, small_cap):
    """與全體差幅 ≥ thr，或為小型股（豁免）。"""
    return ((ret - avg_all).abs() >= thr) | small_cap


def ind_diff_ok(ret, iavg, thr, small_cap):
    """與同類差幅 ≥ thr，或同類未達5家（NaN豁免），或為小型股（豁免）。"""
    d = (ret - iavg).abs()
    return (d >= thr) | d.isna() | small_cap


def prep_ret(close_slice, company, price_filter=True):
    """共用前置：計算漲跌幅，過濾低價股，回傳常用變數。"""
    p_end   = close_slice.iloc[-1]
    p_start = close_slice.iloc[0]
    ret     = (p_end / p_start - 1) * 100

    if price_filter:
        mask = p_end >= MIN_PRICE
        ret  = ret[mask].dropna()
    else:
        ret = ret.dropna()

    cat  = company['產業類別'].reindex(ret.index)
    cap  = company['實收資本額(元)'].reindex(ret.index)
    sc   = (cap < MIN_CAPITAL).fillna(False)
    avg_all = ret.mean()
    iavg    = ind_avg_vec(ret, cat)

    return (ret, p_start.reindex(ret.index), p_end.reindex(ret.index),
            sc, avg_all, iavg)


def bool_series(index, val=False):
    """建立全為 val 的布林 Series。"""
    return pd.Series(val, index=index, dtype=bool)


# ============================================================
# 第1款：近6日累積漲跌幅異常
# ============================================================
def cond_1(close, company):
    if len(close) < 6:
        return bool_series(close.columns)
    ret, p0, p1, sc, avg_all, iavg = prep_ret(close.iloc[-6:], company)
    gap      = (p1 - p0).abs()
    a_ok     = all_diff_ok(ret, avg_all, C1_DIFF, sc)
    i_ok     = ind_diff_ok(ret, iavg, C1_DIFF, sc)
    ca = (ret.abs() > C1_A_RET)  & a_ok & i_ok
    cb = (ret.abs() >= C1_B_RET) & a_ok & i_ok & (gap >= C1_PRICE_GAP)
    return (ca | cb).reindex(close.columns, fill_value=False)


# ============================================================
# 第2款：起迄兩個營業日漲跌幅異常（30/60/90日）
# ============================================================
def cond_2(close, company):
    p_today = close.iloc[-1]
    p_ref   = close.iloc[-2]

    def _one_period(n_days, base_thr, low_thr):
        if len(close) < n_days:
            return bool_series(close.columns)
        ret, _, p1, sc, avg_all, iavg = prep_ret(close.iloc[-n_days:], company)
        thr = pd.Series(base_thr, index=ret.index)
        thr[p1 < MIN_PRICE] = low_thr
        a_ok  = all_diff_ok(ret, avg_all, C2_DIFF, sc)
        i_ok  = ind_diff_ok(ret, iavg,    C2_DIFF, sc)
        ref   = p_ref.reindex(ret.index)
        up   = (ret >   thr) & a_ok & i_ok & (p1 > ref)
        down = (ret < -thr)  & a_ok & i_ok & (p1 < ref)
        return (up | down).reindex(close.columns, fill_value=False)

    c30 = _one_period(30, C2_D30, C2_D30_LOW)
    c60 = _one_period(60, C2_D60, C2_D60_LOW)
    c90 = _one_period(90, C2_D90, C2_D90_LOW)
    return c30 | c60 | c90


# ============================================================
# 第3款：漲跌幅異常 + 成交量放大
# ============================================================
def cond_3(close, volume, company):
    if volume is None or len(close) < 60:
        return bool_series(close.columns)
    ret, _, p1, sc, avg_all, iavg = prep_ret(close.iloc[-6:], company)
    vol_today  = volume.iloc[-1].reindex(ret.index)
    vol_60_avg = volume.iloc[-60:].mean().reindex(ret.index)
    vol_ratio  = (vol_today / vol_60_avg.replace(0, np.nan))
    vol_ratio_all = (volume.iloc[-1] / volume.iloc[-60:].mean().replace(0, np.nan))
    avg_vol_ratio = vol_ratio_all.mean()
    ret_ok = (ret.abs() > C3_RET)
    a_ok   = all_diff_ok(ret, avg_all, C3_DIFF, sc)
    i_ok   = ind_diff_ok(ret, iavg,    C3_DIFF, sc)
    v_ok   = (vol_ratio >= C3_VOL_X) & ((vol_ratio - avg_vol_ratio) >= C3_VOL_DIFF)
    not_excluded = vol_today >= C3_MIN_VOL
    return (ret_ok & a_ok & i_ok & v_ok & not_excluded).reindex(
        close.columns, fill_value=False)


# ============================================================
# 第4款：漲跌幅異常 + 週轉率過高
# ============================================================
def cond_4(close, turnover, company):
    if turnover is None or len(close) < 6:
        return bool_series(close.columns)
    ret, _, _, sc, avg_all, iavg = prep_ret(close.iloc[-6:], company)
    turn_today   = turnover.iloc[-1].reindex(ret.index)
    avg_turn_all = turnover.iloc[-1].mean()
    ret_ok = (ret.abs() > C4_RET)
    a_ok   = all_diff_ok(ret, avg_all, C4_DIFF, sc)
    i_ok   = ind_diff_ok(ret, iavg,    C4_DIFF, sc)
    t_ok   = (turn_today > C4_TURN) & ((turn_today - avg_turn_all).abs() >= C4_TURN_DIFF)
    return (ret_ok & a_ok & i_ok & t_ok).reindex(close.columns, fill_value=False)


# ============================================================
# 第5款：略過（需個別券商交易資料）
# ============================================================
def cond_5(close):
    return bool_series(close.columns)


# ============================================================
# 第6款：本益比/股價淨值比異常 + 週轉率過高 + 集中度
# ============================================================
def cond_6(close, pe, pbr, turnover, volume, company):
    if pe is None or pbr is None or turnover is None or volume is None:
        return bool_series(close.columns)
    stocks   = close.columns
    pe_t     = pe.iloc[-1].reindex(stocks)
    pbr_t    = pbr.iloc[-1].reindex(stocks)
    turn_t   = turnover.iloc[-1].reindex(stocks)
    vol_t    = volume.iloc[-1].reindex(stocks)
    cat      = company['產業類別'].reindex(stocks)
    pe_valid = pe_t[(pe_t > 0) & pe_t.notna()]
    pe_avg   = pe_valid.mean()
    pe_cond  = (pe_t < 0) | ((pe_t >= C6_PE) & (pe_t >= pe_avg * C6_PE_X))
    pbr_avg  = pbr_t[pbr_t.notna()].mean()
    pbr_cond = (pbr_t >= C6_PBR) & (pbr_t >= pbr_avg * C6_PBR_X)
    tv_cond  = (turn_t >= C6_TURN) & (vol_t >= C6_VOL)
    df_pbr   = pd.DataFrame({'pbr': pbr_t, 'cat': cat})
    ind_pbr  = df_pbr.groupby('cat')['pbr'].mean()
    ind_avg_ = df_pbr['cat'].map(ind_pbr)
    ind_pbr_cond = pbr_t >= (ind_avg_ * C6_PBR_IND_X)
    return (pe_cond & pbr_cond & tv_cond & ind_pbr_cond).reindex(
        close.columns, fill_value=False)


# ============================================================
# 第7款：漲跌幅異常 + 券資比放大
# ============================================================
def cond_7(close, mg_buy, mg_sell, mg_buy_rate, mg_sell_rate, company):
    if any(x is None for x in [mg_buy, mg_sell, mg_buy_rate, mg_sell_rate]):
        return bool_series(close.columns)
    if len(close) < 6:
        return bool_series(close.columns)
    ret, _, _, sc, avg_all, iavg = prep_ret(close.iloc[-6:], company)
    buy_prev  = mg_buy.iloc[-2].reindex(ret.index)
    sell_prev = mg_sell.iloc[-2].reindex(ret.index)
    mr_prev = (sell_prev / buy_prev.replace(0, np.nan)) * 100
    buy_6  = mg_buy.iloc[-7:-1].reindex(columns=ret.index)
    sell_6 = mg_sell.iloc[-7:-1].reindex(columns=ret.index)
    mr_6   = (sell_6 / buy_6.replace(0, np.nan)) * 100
    mr_min = mr_6.min()
    mbu_rate = mg_buy_rate.iloc[-2].reindex(ret.index)
    mse_rate = mg_sell_rate.iloc[-2].reindex(ret.index)
    ret_ok = (ret.abs() > C7_RET)
    a_ok   = all_diff_ok(ret, avg_all, C7_DIFF, sc)
    i_ok   = ind_diff_ok(ret, iavg,    C7_DIFF, sc)
    mr_ok  = (mr_prev >= C7_MR) & (mbu_rate >= C7_MBU) & (mse_rate >= C7_MSE)
    exp_ok = mr_prev >= (mr_min * C7_MR_EXP)
    mr_prev2 = (mg_sell.iloc[-3] / mg_buy.iloc[-3].replace(0, np.nan)) * 100
    mr_prev2 = mr_prev2.reindex(ret.index)
    not_excluded = mr_prev >= mr_prev2
    return (ret_ok & a_ok & i_ok & mr_ok & exp_ok & not_excluded).reindex(
        close.columns, fill_value=False)


# ============================================================
# 第8款：臺灣存託憑證溢折價異常（略過）
# ============================================================
def cond_8(close):
    return bool_series(close.columns)


# ============================================================
# 第9款：成交量持續放大
# ============================================================
def cond_9(close, volume, amount):
    if volume is None or len(close) < 60:
        return bool_series(close.columns)
    stocks    = close.columns
    vol_60avg = volume.iloc[-60:].mean()
    vol_6avg  = volume.iloc[-6:].mean()
    vol_today = volume.iloc[-1]
    ratio_6avg = vol_6avg / vol_60avg.replace(0, np.nan)
    ratio_day  = vol_today / vol_60avg.replace(0, np.nan)
    avg_ratio_6avg = ratio_6avg.mean()
    avg_ratio_day  = ratio_day.mean()
    c6avg = (ratio_6avg >= C9_X) & ((ratio_6avg - avg_ratio_6avg) >= C9_DIFF)
    cday  = (ratio_day  >= C9_X) & ((ratio_day  - avg_ratio_day)  >= C9_DIFF)
    not_excl = pd.Series(True, index=stocks)
    not_excl &= (vol_today >= C9_MIN_VOL)
    if amount is not None:
        not_excl &= (amount.iloc[-1] >= C9_MIN_AMT)
    return ((c6avg & cday) & not_excl).reindex(close.columns, fill_value=False)


# ============================================================
# 第10款：累積週轉率過高
# ============================================================
def cond_10(close, turnover, amount):
    if turnover is None or len(close) < 6:
        return bool_series(close.columns)
    stocks       = close.columns
    turn_6sum    = turnover.iloc[-6:].sum()
    turn_today   = turnover.iloc[-1]
    avg_turn6    = turn_6sum.mean()
    avg_turn_day = turn_today.mean()
    c6  = (turn_6sum  > C10_TURN6) & ((turn_6sum  - avg_turn6).abs()    >= C10_TURN6_DIFF)
    cd  = (turn_today >= C10_TURN)  & ((turn_today - avg_turn_day).abs() >= C10_TURN_DIFF)
    not_excl = pd.Series(True, index=stocks)
    if amount is not None:
        not_excl &= (amount.iloc[-1] >= C10_MIN_AMT)
    return (c6 & cd & not_excl).reindex(close.columns, fill_value=False)


# ============================================================
# 第11款：起迄兩日最後成交價「絕對價差」異常
# ============================================================
def cond_11(close):
    if len(close) < 6:
        return bool_series(close.columns)
    p_end   = close.iloc[-1]
    p_start = close.iloc[-6]
    gap     = (p_end - p_start).abs()
    price_level  = ((p_end / C11_STEP).apply(np.floor)).clip(lower=0)
    dyn_thr      = C11_GAP + price_level * C11_STEP_ADD
    high_6 = close.iloc[-6:].max()
    low_6  = close.iloc[-6:].min()
    gap_ok   = gap >= dyn_thr
    is_high  = p_end >= high_6
    is_low   = p_end <= low_6
    return (gap_ok & (is_high | is_low)).reindex(close.columns, fill_value=False)


# ============================================================
# 第12款：借券賣出比率過高
# ============================================================
def cond_12(close, lend, volume):
    if lend is None or volume is None:
        return bool_series(close.columns)
    if len(lend) < 60 or len(volume) < 60:
        return bool_series(close.columns)
    lend_prev  = lend.iloc[-2]
    lend_6sum  = lend.iloc[-7:-1].sum()
    vol_6sum   = volume.iloc[-7:-1].sum()
    lend_60avg = lend.iloc[-61:-1].mean()
    lend_ratio = (lend_6sum  / vol_6sum.replace(0, np.nan)) * 100
    lend_x     = lend_prev   / lend_60avg.replace(0, np.nan)
    not_excl  = (volume.iloc[-2] >= C12_MIN_VOL) & (lend_prev >= C12_MIN_LEND)
    return ((lend_ratio > C12_RATIO) & (lend_x >= C12_X) & not_excl).reindex(
        close.columns, fill_value=False)


# ============================================================
# 第13款：當日沖銷比率過高
# ============================================================
def cond_13(close, daytrade, volume, amount):
    if daytrade is None or volume is None:
        return bool_series(close.columns)
    if len(daytrade) < 6 or len(volume) < 6:
        return bool_series(close.columns)
    stocks = close.columns
    dt_prev   = daytrade.iloc[-2]
    vol_prev  = volume.iloc[-2]
    dt_6sum   = daytrade.iloc[-7:-1].sum()
    vol_6sum  = volume.iloc[-7:-1].sum()
    ratio_6   = (dt_6sum / vol_6sum.replace(0, np.nan)) * 100
    ratio_day = (dt_prev  / vol_prev.replace(0, np.nan)) * 100
    not_excl = (dt_prev >= C13_MIN_DT)
    if amount is not None:
        not_excl &= (amount.iloc[-2] >= C13_MIN_AMT)
    return ((ratio_6 > C13_RATIO) & (ratio_day > C13_RATIO) & not_excl).reindex(
        close.columns, fill_value=False)


# ============================================================
# 輔助：日期轉換
# ============================================================
def _roc_to_ad(d: str) -> str:
    """民國年 'YYY/MM/DD' → 西元年 'YYYY/MM/DD'。"""
    parts = d.strip().split('/')
    if len(parts) == 3:
        try:
            return f"{int(parts[0]) + 1911}/{parts[1]}/{parts[2]}"
        except ValueError:
            pass
    return d.strip()


def _convert_period(period_str: str) -> str:
    """將處置期間字串從民國年轉為西元年。"""
    parts = period_str.split('~')
    converted = []
    for p in parts:
        converted.append(_roc_to_ad(p.strip()))
    return ' ~ '.join(converted)


def _is_period_active(period_ad: str) -> bool:
    """檢查處置期間是否包含今日。"""
    try:
        parts = period_ad.split('~')
        if len(parts) != 2:
            return True
        end_str = parts[1].strip()
        end_date = datetime.strptime(end_str, "%Y/%m/%d").date()
        return date_type.today() <= end_date
    except Exception:
        return True


# ============================================================
# 取得處置中股票
# ============================================================
def fetch_disposed_stocks() -> dict:
    """
    從 TWSE/TPEx API 取得目前處置中的股票清單。
    回傳 {"disposed": {stock_id: {...}}, "fetch_time": "..."}
    """
    disposed = {}
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.twse.com.tw/zh/announcement/notetrans.html',
        'Accept': 'application/json, text/plain, */*',
    }

    # ── 1. 上市 TWSE JSON API ────────────────────────────────
    try:
        url = "https://www.twse.com.tw/rwd/zh/announcement/disposal?response=json"
        resp = requests.get(url, headers=hdrs, timeout=10)
        data = resp.json()

        # Debug：印出 stat 與 fields 幫助排查
        print(f"[TWSE] stat={data.get('stat')!r}  fields={data.get('fields')}  rows={len(data.get('data') or [])}")

        # 動態從 fields 建立欄位 index（更穩健，不寫死）
        fields = data.get("fields") or []
        def _fi(name):
            """從 fields 找欄位位置，找不到回傳 None。"""
            for i, f in enumerate(fields):
                if name in f:
                    return i
            return None

        idx_id   = _fi("股票代號") if fields else None
        idx_name = _fi("股票名稱") if fields else None
        idx_cond = _fi("達") if fields else None          # 達處置標準之情形
        idx_per  = _fi("處置期間") if fields else None
        idx_msr  = _fi("處置措施") if fields else None

        # fallback：若 fields 不存在或對應不到，用預設 index
        # TWSE rwd 格式通常為 7 欄（無序號）：
        # [公告日期(0), 股票代號(1), 股票名稱(2), 異常情事(3), 達處置標準(4), 處置期間(5), 處置措施(6)]
        if idx_id   is None: idx_id   = 1
        if idx_name is None: idx_name = 2
        if idx_cond is None: idx_cond = 4
        if idx_per  is None: idx_per  = 5
        if idx_msr  is None: idx_msr  = 6

        print(f"[TWSE] 欄位索引: 代號={idx_id} 名稱={idx_name} 達標={idx_cond} 期間={idx_per} 措施={idx_msr}")

        stat_ok = data.get("stat") in ("OK", "查詢成功") or (
            isinstance(data.get("stat"), str) and "查無" not in data.get("stat", "")
            and "失敗" not in data.get("stat", "") and data.get("data")
        )

        if stat_ok and data.get("data"):
            for row in data["data"]:
                try:
                    if len(row) <= idx_id:
                        continue
                    stock_id = str(row[idx_id]).strip()
                    name     = str(row[idx_name]).strip() if len(row) > idx_name else ""
                    cond_txt = str(row[idx_cond]).strip() if len(row) > idx_cond else ""
                    period   = str(row[idx_per]).strip()  if len(row) > idx_per  else ""
                    measures = str(row[idx_msr]).strip()  if len(row) > idx_msr  else ""

                    # 股票代號應為 4~6 位數字，做基本驗證
                    if not stock_id or not stock_id[:4].isdigit():
                        continue

                    period_ad = _convert_period(period) if period else ""

                    if _is_period_active(period_ad):
                        disposed[stock_id] = {
                            "name": name,
                            "period": period_ad,
                            "condition": cond_txt,
                            "measures": measures[:120] if measures else "",
                            "source": "TWSE"
                        }
                except Exception as row_e:
                    print(f"[TWSE] row 解析失敗: {row_e}  row={row}")
                    continue
        print(f"✅ TWSE 處置股票: {len([k for k,v in disposed.items() if v.get('source')=='TWSE'])} 檔")
    except Exception as e:
        print(f"⚠️  TWSE 處置資料取得失敗: {e}")

    # ── 2. 上櫃 TPEx JSON API ──────────────────────────────
    try:
        tpex_hdrs = {**hdrs, 'Referer': 'https://www.tpex.org.tw/web/stock/attention/disposal/disposal_query.php?l=zh-tw'}
        url = "https://www.tpex.org.tw/web/stock/attention/disposal/disposal_result.php?l=zh-tw&o=json"
        resp = requests.get(url, headers=tpex_hdrs, timeout=10)
        data = resp.json()

        rows = data.get("aaData") or data.get("data") or []
        print(f"[TPEx] rows={len(rows)}  keys={list(data.keys())[:6]}")

        for row in rows:
            try:
                # TPEx 格式通常為 8+ 欄（有序號）：
                # [序號(0), 公告日期(1), 股票代號(2), 股票名稱(3), 異常情事(4), 達處置標準(5), 處置期間(6), 處置措施(7)]
                stock_id = str(row[2]).strip() if len(row) > 2 else ""
                name     = str(row[3]).strip() if len(row) > 3 else ""
                period   = str(row[6]).strip() if len(row) > 6 else ""
                measures = str(row[7]).strip() if len(row) > 7 else ""

                if not stock_id or not stock_id[:4].isdigit():
                    continue

                period_ad = _convert_period(period) if period else ""

                if _is_period_active(period_ad) and stock_id not in disposed:
                    disposed[stock_id] = {
                        "name": name,
                        "period": period_ad,
                        "measures": measures[:120] if measures else "",
                        "source": "TPEx"
                    }
            except Exception as row_e:
                print(f"[TPEx] row 解析失敗: {row_e}  row={row}")
                continue
        print(f"✅ TPEx 處置股票: {len([k for k,v in disposed.items() if v.get('source')=='TPEx'])} 檔")
    except Exception as e:
        print(f"⚠️  TPEx 處置資料取得失敗: {e}")

    # ── 3. FinLab disposal_information（補充）──────────────
    try:
        init_finlab()
        from finlab import data as finlab_data
        disp_df = finlab_data.get('disposal_information')
        if disp_df is not None and not disp_df.empty:
            import pandas as pd
            
            # 將可能在 index 的欄位 (如 stock_id, date) 攤平
            disp_df = disp_df.reset_index()
            
            # 定義可能的欄位名稱
            end_cols = ['處置結束日期', 'end_date', 'disposal_end_date']
            start_cols = ['處置開始日期', 'start_date', 'disposal_start_date']
            id_cols = ['股票代號', 'stock_id', 'symbol', 'code']
            name_cols = ['股票名稱', 'name', 'stock_name']
            measure_cols = ['處置措施', 'measure', 'status', 'condition']
            
            # 找到實際存在的欄位
            end_col = next((c for c in end_cols if c in disp_df.columns), None)
            start_col = next((c for c in start_cols if c in disp_df.columns), None)
            id_col = next((c for c in id_cols if c in disp_df.columns), None)
            name_col = next((c for c in name_cols if c in disp_df.columns), None)
            m_col = next((c for c in measure_cols if c in disp_df.columns), None)
            
            if id_col and end_col:
                # 轉成 datetime 確保無論原本型態是字串、Timestamp 或 date 都能正確比對
                disp_df['_end_dt'] = pd.to_datetime(disp_df[end_col], errors='coerce')
                # 以今日 00:00:00 作為門檻
                today_dt = pd.to_datetime(date_type.today())
                
                # 篩選未過期的處置（這也會過濾掉 NaT 也就是原本沒日期的列）
                active = disp_df[disp_df['_end_dt'] >= today_dt]
                
                for _, row in active.iterrows():
                    sid = str(row[id_col]).strip()
                    if sid and sid != "nan":
                        if sid not in disposed:
                            start = str(row[start_col]).strip() if start_col else ''
                            end   = str(row[end_col]).strip()
                            
                            measures = ""
                            if m_col:
                                m_val = str(row[m_col]).strip()
                                if m_val and m_val != "nan":
                                    measures = m_val[:120]
                                    
                            name_val = str(row[name_col]).strip() if name_col else sid
                            if name_val == "nan": name_val = sid
                            
                            disposed[sid] = {
                                "name": name_val,
                                "period": f"{start} ~ {end}",
                                "measures": measures,
                                "source": "FinLab"
                            }
            print(f"✅ FinLab 處置補充: {len([k for k,v in disposed.items() if v.get('source')=='FinLab'])} 檔")
    except Exception as e:
        print(f"ℹ️  FinLab 處置資料失敗/略過: {e}")

    return {"disposed": disposed, "fetch_time": datetime.now().isoformat()}


# ============================================================
# 輔助：取得股票名稱
# ============================================================
def _get_stock_name(stock_id, company):
    if company is None or company.empty:
        return str(stock_id)
        
    try:
        # 情境 1: stock_id 在 index 裡
        if str(stock_id) in company.index:
            name = company.loc[str(stock_id), '公司簡稱']
            if isinstance(name, pd.Series):
                name = name.dropna().iloc[-1] if not name.dropna().empty else name.iloc[0]
            if pd.notna(name):
                return str(name)
                
        # 情境 2: stock_id 是 MultiIndex 的其中一層 (例如 date, stock_id)
        if 'stock_id' in company.index.names:
            try:
                matches = company.xs(str(stock_id), level='stock_id')
                if not matches.empty and '公司簡稱' in matches.columns:
                    name = matches.iloc[-1]['公司簡稱']
                    if pd.notna(name):
                        return str(name)
            except KeyError:
                pass
                
        # 情境 3: stock_id 是一個欄位 (column)
        id_col = next((c for c in ['stock_id', '股票代號', 'symbol', 'code'] if c in company.columns), None)
        name_col = next((c for c in ['公司簡稱', 'name', 'stock_name', '股票名稱'] if c in company.columns), None)
        
        if id_col and name_col:
            # 確保型別都是字串以進行比對
            matches = company[company[id_col].astype(str) == str(stock_id)]
            if not matches.empty:
                name = matches.iloc[-1][name_col]
                if pd.notna(name):
                    return str(name)
                    
    except Exception as e:
        print(f"Name resolve error for {stock_id}: {e}")
        pass
        
    return str(stock_id)


def _calc_ret(close, stock_id, days):
    try:
        if len(close) < days:
            return None
        p0 = close.iloc[-days].get(stock_id, None)
        p1 = close.iloc[-1].get(stock_id, None)
        if p0 is not None and p1 is not None and not np.isnan(p0) and not np.isnan(p1) and p0 > 0:
            return round(float((p1 / p0 - 1) * 100), 2)
    except Exception:
        pass
    return None


def _safe_float(val):
    """安全轉換為 float，NaN/None → None。"""
    if val is None:
        return None
    try:
        f = float(val)
        return round(f, 2) if not np.isnan(f) else None
    except (ValueError, TypeError):
        return None


# ============================================================
# 主掃描函式
# ============================================================
def _run_scan():
    """實際執行全條款掃描。"""
    global _scan_response, _scan_date, _condition_series, _loaded_data

    init_finlab()

    # 載入所有資料
    close      = load(F_CLOSE)
    volume     = load(F_VOLUME)
    amount     = load(F_AMOUNT)
    company    = load(F_COMPANY)
    pe         = load(F_PE)
    pbr        = load(F_PBR)
    mg_buy     = load(F_MG_BUY)
    mg_sell    = load(F_MG_SELL)
    mg_buy_rate = load(F_MG_BUY_RATE)
    mg_sell_rate = load(F_MG_SELL_RATE)
    lend       = load(F_LEND)
    daytrade   = load(F_DAYTRADE)

    if close is None or company is None:
        raise ValueError("無法載入收盤價或公司基本資料")

    # 計算週轉率（成交股數 / 已發行股數 × 100%）
    turnover = None
    try:
        shares_outstanding = company['實收資本額(元)'] / 10  # 面額10元
        turnover = volume.div(shares_outstanding, axis=1) * 100
    except Exception as e:
        print(f"⚠️  週轉率計算失敗: {e}")

    _loaded_data = {
        "close": close, "volume": volume, "amount": amount,
        "company": company, "pe": pe, "pbr": pbr,
        "mg_buy": mg_buy, "mg_sell": mg_sell,
        "mg_buy_rate": mg_buy_rate, "mg_sell_rate": mg_sell_rate,
        "lend": lend, "daytrade": daytrade, "turnover": turnover
    }

    # 執行所有條款
    print("🔍 開始執行全條款掃描...")
    c1  = cond_1(close, company)
    print("  ✓ 第1款完成")
    c2  = cond_2(close, company)
    print("  ✓ 第2款完成")
    c3  = cond_3(close, volume, company)
    print("  ✓ 第3款完成")
    c4  = cond_4(close, turnover, company)
    print("  ✓ 第4款完成")
    c5  = cond_5(close)
    c6  = cond_6(close, pe, pbr, turnover, volume, company)
    print("  ✓ 第6款完成")
    c7  = cond_7(close, mg_buy, mg_sell, mg_buy_rate, mg_sell_rate, company)
    print("  ✓ 第7款完成")
    c8  = cond_8(close)
    c9  = cond_9(close, volume, amount)
    print("  ✓ 第9款完成")
    c10 = cond_10(close, turnover, amount)
    print("  ✓ 第10款完成")
    c11 = cond_11(close)
    print("  ✓ 第11款完成")
    c12 = cond_12(close, lend, volume)
    print("  ✓ 第12款完成")
    c13 = cond_13(close, daytrade, volume, amount)
    print("  ✓ 第13款完成")

    _condition_series = {
        1: c1, 2: c2, 3: c3, 4: c4, 5: c5, 6: c6, 7: c7,
        8: c8, 9: c9, 10: c10, 11: c11, 12: c12, 13: c13
    }

    # 實際參與檢測的條款（排除第5、8款）
    active_conds = {k: v for k, v in _condition_series.items() if k not in [5, 8]}

    # 彙總結果
    results = []
    by_condition = {}

    for stock_id in close.columns:
        triggered = []
        for num, series in active_conds.items():
            try:
                if series.get(stock_id, False):
                    triggered.append(num)
            except Exception:
                continue

        if triggered:
            name = _get_stock_name(stock_id, company)
            price = _safe_float(close.iloc[-1].get(stock_id, None))
            ret_6d = _calc_ret(close, stock_id, 6)

            results.append({
                "stock_id": stock_id,
                "name": name,
                "conditions": triggered,
                "count": len(triggered),
                "price": price,
                "ret_6d": ret_6d
            })

            for c in triggered:
                key = str(c)
                by_condition[key] = by_condition.get(key, 0) + 1

    results.sort(key=lambda x: x["count"], reverse=True)

    today = date_type.today().isoformat()
    _scan_response = {
        "scan_date": today,
        "total_flagged": len(results),
        "by_condition": by_condition,
        "stocks": results
    }
    _scan_date = today
    print(f"✅ 掃描完成：共 {len(results)} 檔觸發警示")


def check_all_conditions() -> dict:
    """執行全條款掃描，回傳結果（自動快取當日）。"""
    today = date_type.today().isoformat()
    if _scan_response is not None and _scan_date == today:
        return _scan_response
    _run_scan()
    return _scan_response


def check_single_stock(stock_id: str) -> dict | None:
    """查詢個股警示狀態。若掃描尚未執行，會先執行掃描。"""
    # 確保掃描結果存在
    today = date_type.today().isoformat()
    if _scan_response is None or _scan_date != today:
        _run_scan()

    close   = _loaded_data.get("close")
    company = _loaded_data.get("company")

    if close is None or stock_id not in close.columns:
        return None

    # 從快取的條件結果取得各條款結果
    all_conditions = {}
    triggered = []
    for num, series in _condition_series.items():
        if num in [5, 8]:
            all_conditions[str(num)] = None  # 略過
        else:
            try:
                val = bool(series.get(stock_id, False))
            except Exception:
                val = False
            all_conditions[str(num)] = val
            if val:
                triggered.append(num)

    name = _get_stock_name(stock_id, company)
    price = _safe_float(close.iloc[-1].get(stock_id, None))
    ret_6d  = _calc_ret(close, stock_id, 6)
    ret_30d = _calc_ret(close, stock_id, 30)
    ret_60d = _calc_ret(close, stock_id, 60)
    ret_90d = _calc_ret(close, stock_id, 90)

    # 附加指標
    metrics = {}
    volume   = _loaded_data.get("volume")
    turnover = _loaded_data.get("turnover")
    pe_data  = _loaded_data.get("pe")
    pbr_data = _loaded_data.get("pbr")

    if volume is not None and stock_id in volume.columns:
        vol_today = _safe_float(volume.iloc[-1].get(stock_id, 0))
        metrics["volume"] = vol_today
        if len(volume) >= 60:
            vol_60avg = _safe_float(volume.iloc[-60:].mean().get(stock_id, 0))
            metrics["vol_60avg"] = vol_60avg
            if vol_60avg and vol_60avg > 0:
                metrics["vol_ratio"] = round(vol_today / vol_60avg, 2)

    if turnover is not None and stock_id in turnover.columns:
        metrics["turnover"] = _safe_float(turnover.iloc[-1].get(stock_id, 0))

    if pe_data is not None and stock_id in pe_data.columns:
        metrics["pe"] = _safe_float(pe_data.iloc[-1].get(stock_id, None))

    if pbr_data is not None and stock_id in pbr_data.columns:
        metrics["pbr"] = _safe_float(pbr_data.iloc[-1].get(stock_id, None))

    return {
        "stock_id": stock_id,
        "name": name,
        "price": price,
        "ret_6d": ret_6d,
        "ret_30d": ret_30d,
        "ret_60d": ret_60d,
        "ret_90d": ret_90d,
        "metrics": metrics,
        "conditions_triggered": triggered,
        "all_conditions": all_conditions,
        "total_triggered": len(triggered)
    }
