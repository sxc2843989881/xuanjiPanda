"""B4V9 线上信号页对拍验证：signal_page_b4/generate.py vs 策略目录信号生成器

验证目标：
  线上自包含页面（GitHub Actions 运行，无 Mongo/无本地路径）与官方引擎同源的
  signal_generator_dct.py 在完全相同的指数数据上，逐日 direction / weight 必须
  一致（允许 1e-6 浮点容差）。

两组对拍：
  A. 生产配置 fw=35：线上 B4_PARAMS vs b4v9 BASELINE_PARAMS
  B. 回归口径 fw=20：线上临时改 fw=20 vs b4v8 生成器 fw=20（证明 AW 移植本身忠实）

另附：signal_page_b4/data 缓存与 c:\\temp_v72_data 引擎数据的收盘价比值稳定性检查。
"""
import os
import sys
import importlib.util

import numpy as np
import pandas as pd

ROOT = r'c:\XuanJLH\pandaai'
PAGE = os.path.join(ROOT, 'signal_page_b4', 'generate.py')
B4V9_DIR = os.path.join(ROOT, 'strategies', 'growth_value_rotation_b4v9')
B4V8_DIR = os.path.join(ROOT, 'strategies', 'growth_value_rotation_b4v8')
PAGE_DATA = os.path.join(ROOT, 'signal_page_b4', 'data')
ENGINE_DATA = r'c:\temp_v72_data'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_pair(data_dir):
    g = pd.read_csv(os.path.join(data_dir, 'index_480080.csv'))
    v = pd.read_csv(os.path.join(data_dir, 'index_480081.csv'))
    for d in (g, v):
        d['date'] = pd.to_datetime(d['date'])
        d['close'] = pd.to_numeric(d['close'], errors='coerce')
    g_s = g.set_index('date')['close'].astype(float).sort_index().dropna()
    v_s = v.set_index('date')['close'].astype(float).sort_index().dropna()
    common = g_s.index.intersection(v_s.index)
    return g_s.loc[common].sort_index(), v_s.loc[common].sort_index()


def import_strat(strat_dir):
    """导入指定策略目录的 params + signal_generator_dct（先清缓存避免同名模块串用）"""
    for m in ('params', 'signal_generator_dct'):
        sys.modules.pop(m, None)
    if strat_dir in sys.path:
        sys.path.remove(strat_dir)
    sys.path.insert(0, strat_dir)
    import params
    import signal_generator_dct
    return params, signal_generator_dct


def compare(label, page_mod, page_params, gen_mod, strat_params, g, v):
    page_df = page_mod.compute_signals(g, v, page_params)[['dir', 'wt']].copy()
    _, dir_series, wt_s = gen_mod.generate_signals(
        g, v, {**strat_params, '_debug_dir': True})
    strat = pd.DataFrame({'dir_s': dir_series, 'wt_s': wt_s})
    strat.index = pd.to_datetime(strat.index)

    merged = page_df.join(strat, how='inner')
    n = len(merged)
    dir_bad = merged[merged['dir'] != merged['dir_s']]
    wt_diff = (merged['wt'] - merged['wt_s']).abs()
    wt_bad = merged[wt_diff > 1e-6]

    print(f'\n[{label}] 对齐交易日 {n} 天 ({merged.index[0].date()} ~ {merged.index[-1].date()})')
    print(f'  方向不一致: {len(dir_bad)} 天')
    print(f'  权重不一致(>1e-6): {len(wt_bad)} 天, 最大绝对差 {wt_diff.max():.2e}')
    if len(dir_bad):
        print(dir_bad.head(10))
    if len(wt_bad):
        print(merged.loc[wt_bad.index].assign(diff=wt_diff[wt_bad.index]).head(10))
    ok = (len(dir_bad) == 0 and len(wt_bad) == 0)
    print(f'  结论: {"✅ 逐日完全一致" if ok else "❌ 存在分歧"}')
    return ok, page_df


def main():
    print('=' * 64)
    print('  B4V9 线上信号页对拍验证')
    print('=' * 64)

    # ---- 0. 数据源比值稳定性：页面缓存 vs 引擎 CSV ----
    pg, pv = load_pair(PAGE_DATA)
    eg, ev = load_pair(ENGINE_DATA)
    for name, ps, es in (('成长100', pg, eg), ('价值100', pv, ev)):
        ov = ps.index.intersection(es.index)
        ratio = (ps.loc[ov] / es.loc[ov]).replace([np.inf, -np.inf], np.nan).dropna()
        print(f'  数据 {name}: 页面{len(ps)}天/引擎{len(es)}天, 重叠{len(ov)}天, '
              f'收盘比值均值={ratio.mean():.6f} 标准差={ratio.std():.2e}')

    # 对拍统一使用页面缓存数据（信号基于比价/标准化，量纲不影响结果）
    g, v = pg, pv
    page_mod = load_module('signal_page_generate', PAGE)

    # ---- A. 生产配置 fw=35：线上 vs b4v9 ----
    p9, gen9 = import_strat(B4V9_DIR)
    ok_a, page_a = compare('A. 生产 fw=35 线上B4_PARAMS vs b4v9',
                           page_mod, page_mod.B4_PARAMS, gen9,
                           p9.BASELINE_PARAMS, g, v)
    print(f'  参数核对: aw_flip_window 页面={page_mod.B4_PARAMS["aw_flip_window"]} '
          f'b4v9={p9.BASELINE_PARAMS["aw_flip_window"]}; '
          f'dr_mode 页面={page_mod.B4_PARAMS["dr_mode"]} '
          f'b4v9={p9.BASELINE_PARAMS["dr_mode"]}')

    # ---- B. 回归 fw=20：线上(临时fw20) vs b4v8 ----
    p8, gen8 = import_strat(B4V8_DIR)
    page_params_fw20 = {**page_mod.B4_PARAMS, 'aw_flip_window': 20}
    ok_b, _ = compare('B. 回归 fw=20 线上(临时fw20) vs b4v8',
                      page_mod, page_params_fw20, gen8,
                      p8.BASELINE_PARAMS, g, v)

    # ---- C. 最新10日信号速览 ----
    print('\n[C. 最新10个交易日信号（线上页面口径）]')
    for d, row in page_a.tail(10).iterrows():
        print(f'  {d.date()}  dir={row["dir"]:<6}  wt={row["wt"]:.4f}')

    print('\n' + '=' * 64)
    print(f'  总结: A(fw35生产)={"PASS" if ok_a else "FAIL"}  '
          f'B(fw20回归)={"PASS" if ok_b else "FAIL"}')
    print('=' * 64)
    sys.exit(0 if (ok_a and ok_b) else 1)


if __name__ == '__main__':
    main()
