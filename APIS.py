# -*- coding: utf-8 -*-
"""
Created on Tue Jul  1 13:51:36 2025

@author: jeffrey.chuang
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from itertools import combinations

# 針對紀錄後大表的單一股票的原始資料進行爬蟲
def scan_for_Robot(df, bs_type: str, cv_thres: float = 0.399):
    # 此處優化原本程式碼中將 cv_thres 定義為 int() 的錯誤 type hint
    if not isinstance(df, pd.DataFrame) or not isinstance(bs_type, str):
        print('🛑 Wrong data type in params.')
        return pd.DataFrame() # 回傳空的 DataFrame，避免後續 .empty 報錯

    if df.empty:
        return pd.DataFrame()

    # 清理合併
    def clear_df(df: pd.DataFrame):
        df = df.copy()  # 複製一份，避免修改原始 df
        df['datetime'] = pd.to_datetime(df['datetime'])
        result = df.groupby(['datetime', 'b_s_type']).agg(
            size=('size', 'sum'),
            price=('price', lambda x: (x * df.loc[x.index, 'size']).sum() / df.loc[x.index, 'size'].sum() if df.loc[x.index, 'size'].sum() != 0 else np.nan)
        ).reset_index()
        df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce').dt.floor('s')
        return result

    def seconds_com_unique_list(df_time: pd.DataFrame, tres: int = 61):
        dt_objects = [ts.replace(microsecond=0) for ts in df_time]
        pairs = list(combinations(dt_objects, 2))

        filtered_results = []
        for t1, t2 in pairs:
            diff_seconds = abs((t2 - t1).total_seconds())
            if diff_seconds < tres:
                filtered_results.append(int(diff_seconds))

        return list(set(filtered_results))

    def merge_close_values(arr):
        arr = sorted(arr)
        result = []
        skip_indices = set()

        for i in range(len(arr)):
            if i in skip_indices:
                continue
            current = arr[i]
            if current <= 10:
                result.append(current)
            else:
                merged = False
                for j in range(i + 1, len(arr)):
                    if arr[j] > 10 and abs(arr[j] - current) / current <= 0.1:
                        result.append(max(current, arr[j]))
                        skip_indices.add(j)
                        merged = True
                        break
                if not merged:
                    result.append(current)
        return np.array(sorted(set(result)))

    raw_df = clear_df(df)
    raw_df['datetime_trimmed'] = raw_df['datetime'].dt.floor('s')

    BoS_trade = raw_df[raw_df['b_s_type'] == bs_type]

    res_df = pd.DataFrame(columns=['Size', 'Time', 'temp_cv', 'vol_hhour', 'vol_EoD'])

    if len(BoS_trade) < 10:
        return res_df

    # 第一步：從買賣別進行區分，前一分鐘的純買或賣
    first_min_trade_time = BoS_trade['datetime'].iloc[0]
    last_min_trade_time = first_min_trade_time + timedelta(seconds=61)

    # for loop starting dataframe #
    trade_scan_Head_df = BoS_trade[(BoS_trade['datetime'] >= first_min_trade_time) & (BoS_trade['datetime'] <= last_min_trade_time)]
    
    if trade_scan_Head_df.empty:
        return res_df
        
    BoS_trade_size_unique_temp = np.sort(trade_scan_Head_df['size'].unique())
    BoS_trade_size_unique_temp_temp = BoS_trade_size_unique_temp[~np.isin(BoS_trade_size_unique_temp, [1])]
    BoS_trade_size_unique = merge_close_values(BoS_trade_size_unique_temp_temp)

    for current_trade_index in trade_scan_Head_df.index:
        for single_Size in BoS_trade_size_unique:

            scale_tolerance = 0.095
            trade_scan_Body_df = BoS_trade[(BoS_trade.index > current_trade_index) & (BoS_trade['size'] >= single_Size * (1 - scale_tolerance)) & (BoS_trade['size'] <= single_Size * (1 + scale_tolerance))]
            time_error = timedelta(seconds=1)
            
            if trade_scan_Body_df.empty:
                continue

            # 接下來找時間區段資訊
            unique_seconds = seconds_com_unique_list(trade_scan_Body_df['datetime_trimmed'], tres=61)
            filtered_sorted_seconds = list(map(int, sorted(set(unique_seconds) - {0, 1, 2, 3})))

            for time_interval in filtered_sorted_seconds:
                if len(trade_scan_Body_df) == 0:
                    continue

                counter_yes = 0
                counter_all = 1

                next_trade_timestamp = BoS_trade.loc[current_trade_index]['datetime_trimmed'] + timedelta(seconds=time_interval)

                while next_trade_timestamp <= trade_scan_Body_df['datetime_trimmed'].iloc[-1]:
                    counter_all += 1
                    next_trade_timestamp = next_trade_timestamp + timedelta(seconds=time_interval)

                    possible_trade_timestamp = [next_trade_timestamp, next_trade_timestamp + time_error]

                    trade_scan_body_datetimes = trade_scan_Body_df['datetime_trimmed'].to_list()
                    if possible_trade_timestamp[1] in trade_scan_body_datetimes:
                        next_trade_timestamp = possible_trade_timestamp[1]
                        counter_yes += 1
                    elif possible_trade_timestamp[0] in trade_scan_body_datetimes:
                        next_trade_timestamp = possible_trade_timestamp[0]
                        counter_yes += 1

                cv_caled = counter_yes / counter_all

                if cv_caled > cv_thres:
                    time_now = datetime.now()
                    timestamp_hhl = time_now + timedelta(minutes=30)
                    timestamp_EoD = time_now.replace(hour=13, minute=30, second=0, microsecond=0)
                    seconds_remains = int((timestamp_EoD - time_now).total_seconds())

                    if time_now > time_now.replace(hour=13, minute=25, second=0, microsecond=0):
                        timestamp_hhl = timestamp_EoD

                    # Prevent division by zero
                    if time_interval > 0:
                        forecast_vol_1 = int(seconds_remains / time_interval * single_Size)
                        forecast_vol_hhl = int((timestamp_hhl - time_now).total_seconds() / time_interval * single_Size)
                    else:
                        forecast_vol_1 = 0
                        forecast_vol_hhl = 0
                        
                    forecast_vol_2 = int(BoS_trade['size'].sum() * seconds_remains / 180)

                    forecast_vol_EoD = min(forecast_vol_1, forecast_vol_2)
                    

                    temp_res = pd.DataFrame([{'Size': single_Size, 'Time': time_interval, 'temp_cv': cv_caled, 'vol_hhour': forecast_vol_hhl, 'vol_EoD': forecast_vol_EoD}])

                    if not temp_res.empty and not temp_res.isna().all().all():
                        temp_res = temp_res.dropna(axis=1, how='all')
                        temp_res = temp_res.astype(res_df.dtypes.to_dict())
                        res_df = pd.concat([res_df, temp_res], ignore_index=True)

    if res_df.empty:
        return res_df

    unique_df = res_df.drop_duplicates(subset=['Size', 'Time'])
    unique_df = unique_df.reset_index(drop=True)

    def algo_for_unique_df(df: pd.DataFrame):
        uniqe_size = sorted(df['Size'].unique().tolist())
        res_df = pd.DataFrame(columns=df.columns)

        for i in uniqe_size:
            specified_size_df = df[df['Size'] == i]
            time_stamps = specified_size_df['Time']

            if (time_stamps.min() > 30) and ((time_stamps.max() / time_stamps.min()) < 1.04):
                res_df = pd.concat([res_df, df[(df['Size'] == i) & (df['Time'] == time_stamps.min())]])
            else:
                time_stamps_ls = time_stamps.tolist()
                neighborhood_set = set()
                for num in time_stamps_ls:
                    neighborhood_set.update([num - 1, num, num + 1])

                def count_multiples(candidate, numbers):
                    # Prevent division by zero
                    if candidate == 0:
                        return 0
                    return sum(1 for n in numbers if n % candidate == 0)

                multiples_count = {a: count_multiples(a, time_stamps_ls) for a in neighborhood_set}
                if multiples_count:
                    best_a = max(multiples_count, key=lambda x: multiples_count[x])
                    res_df = pd.concat([res_df, df[(df['Size'] == i) & (df['Time'] == best_a)]])

        return res_df
        
    if not unique_df.empty:
        final_df = algo_for_unique_df(unique_df).reset_index(drop=True)
    else:
        final_df = unique_df

    return final_df
