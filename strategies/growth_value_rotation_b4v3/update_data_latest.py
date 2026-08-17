"""从cnindex.com.cn获取480080/480081数据并更新到最新日期"""
import requests
import pandas as pd
import os
from datetime import datetime

def fetch_index_data(index_code, start_date="2012-01-01", end_date=None):
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    url = "https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
    params = {
        "indexCode": index_code,
        "startDate": start_date,
        "endDate": end_date,
        "frequency": "day",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "http://www.cnindex.com.cn/",
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    data = r.json()
    assert data.get("code") == 200, f"API返回code={data.get('code')} {data}"
    raw = data.get("data", {})
    rows = raw.get("data", [])
    assert len(rows) > 0, "数据为空"
    df = pd.DataFrame(rows, columns=raw["item"])
    df["date"] = pd.to_datetime(df["timestamp"])
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df

print("=" * 60)
print("更新成长100(480080)和价值100(480081)数据到最新")
print("=" * 60)

END = datetime.now().strftime("%Y-%m-%d")
fetched = {}
for code, name in [("480080", "成长100"), ("480081", "价值100")]:
    print(f"\n{name}({code}): 正在获取...")
    df = fetch_index_data(code, "2012-01-01", END)
    print(f"  OK {len(df)} 行, {df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"  最新5条交易日:")
    for _, row in df.tail(5).iterrows():
        print(f"    {row['date'].strftime('%Y-%m-%d')}  开:{row['open']:.2f}  收:{row['close']:.2f}  ({row.get('percent', '')})")
    fetched[code] = df

targets = {
    "V72项目数据目录": r'c:\XuanJLH\Qbot\custom\research\成长价值轮动\v72\项目代码\data',
    "sxc data_module数据目录": r'c:\caches\sxc\style_rotation_strategy\data_module\data',
    "临时目录(回测用)": r'c:\temp_v72_data',
}

for label, path in targets.items():
    os.makedirs(path, exist_ok=True)
    print(f"\n同步到 {label}: {path}")
    for code, name in [("480080", "成长100"), ("480081", "价值100")]:
        out_path = os.path.join(path, f'index_{code}.csv')
        fetched[code].to_csv(out_path, index=False, encoding='utf-8-sig')
        print(f"  {name}: {out_path}")

print("\n" + "=" * 60)
print("数据更新完成！")
print(f"成长100最新: {fetched['480080'].iloc[-1]['date'].strftime('%Y-%m-%d')}  收盘 {fetched['480080'].iloc[-1]['close']:.2f}")
print(f"价值100最新: {fetched['480081'].iloc[-1]['date'].strftime('%Y-%m-%d')}  收盘 {fetched['480081'].iloc[-1]['close']:.2f}")