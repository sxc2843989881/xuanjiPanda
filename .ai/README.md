# PandaAI AI 开发文档

> PandaAI 量化回测平台的 AI 协作文档，记录引擎使用方式、策略开发流程和回测工作流。

## 文档导航

| 文档 | 用途 |
|------|------|
| `../strategies/growth_value_rotation/PROJECT_GUIDE.md` | **★ 策略工程总纲** — 任何策略的入口文档，含目录结构、调用链、一键命令 |
| `../strategies/growth_value_rotation/DATA_PIPELINE.md` | **数据流水线** — 数据来源、更新流程、缩放原理、预检机制 |
| `workflows/run_backtest.md` | **引擎回测通用流程** — 从信号生成到结果读取的端到端指南 |
| `../panda_quantflow/docs/STRATEGY_GUIDE.md` | 策略开发指南（引擎内置） |
| `../panda_quantflow/docs/BACKTEST_CONFIG.md` | 回测参数配置（引擎内置） |
| `../panda_quantflow/docs/ARCHITECTURE.md` | 引擎架构文档（引擎内置） |
| `../strategies/` | **策略开发目录** — 所有策略代码存放于此 |
| `../strategies/growth_value_rotation/` | B1-木星 成长价值轮动策略 |
| `../strategies/growth_value_rotation_b4v3/` | **B4V3 成长价值轮动**（基线，45.73%/Sharpe1.806） |
| `../strategies/growth_value_rotation_b4v4/` | **B4V4 成长价值轮动（L5锁满全程版）** — 年化46.51%/Sharpe1.844（旧线上信号页口径，已被 B4V9 接替） |
| `../strategies/growth_value_rotation_b4v5/` | **B4V5 成长价值轮动（+第10层反打脸保护 AW·slow_dc）** — 年化45.31%/Sharpe1.792 |
| `../strategies/growth_value_rotation_b4v6/` | **B4V6 成长价值轮动（+第11层单腿急跌·轮动减仓 RCD）** — 年化45.48%/Sharpe1.804 |
| `../strategies/growth_value_rotation_b4v7/` | **B4V7 成长价值轮动**（第12层 L5R 已撤回/默认关闭）— 默认参数回退至 B4V6 基线：年化45.48%/Sharpe1.804 |
| `../strategies/growth_value_rotation_b4v8/` | **B4V8 成长价值轮动**（+第14层震荡不明朗·半仓 CHOP，已证伪/默认关闭）— 默认回退 B4V7 基线：年化45.48%/Sharpe1.804。AW 非对称确认（第六阶段）已证伪/默认关闭 |
| `../strategies/growth_value_rotation_b4v9/` | **★ B4V9 成长价值轮动（现行生产/线上信号页）** — B4V8 默认基线 + 唯一改动 `aw_flip_window 20→35`：年化45.77%/最大回撤-22.03%/Calmar2.08（帕累托改进）；截至2026-11-06 年化44.97%/-22.03%。含 `verify_signal_page.py` 线上页对拍（3361天逐日零差异） |
| `../strategies/growth_value_rotation_b5u_932304/` | **B5U-932304 杠杆策略** — 年化96.77%（2x杠杆） |
| `../strategies/growth_value_rotation_b5u_932304_mtv/` | **V113 B5U-932304-MTV** — 动量趋势轮动 + L5保护（年化59.44%） |
| `../research/b1_improvements/` | B1 木星策略改进回测记录 |

## 策略开发目录

**所有策略代码存放在 `../strategies/` 目录下**，每个策略一个子目录：

```
pandaai/strategies/
├── growth_value_rotation/     # B1-木星 成长价值轮动策略
│   ├── PROJECT_GUIDE.md       # ★ 工程总纲（入口文档）
│   ├── DATA_PIPELINE.md       # 数据流水线文档
│   ├── README.md              # 策略说明文档
│   ├── params.py              # 参数配置
│   ├── signal_generator.py    # 信号生成器（6层过滤 → df_factor）
│   ├── strategy_shell.py      # PandaAI策略壳代码 + 回测函数
│   ├── precheck.py            # 数据预检模块
│   ├── update_data.py         # 数据更新模块（ETF缩放回填）
│   ├── run_backtest.py        # ★ 一键回测脚本
│   ├── df_factors/            # 生成的信号文件
│   └── results/               # 回测结果
├── growth_value_rotation_b5u_932304/  # B5U-932304（年化96.77%）
│   ├── params.py              # 参数配置
│   ├── signal_generator.py    # B4V2+U层比价动量信号生成
│   ├── strategy_shell.py      # 策略壳代码（含杠杆支持）
│   ├── strategy_shell_leveraged.py # 2x杠杆策略壳
│   ├── run_backtest.py        # 一键回测
│   ├── run_daily.py           # 每日执行脚本
│   ├── df_factors/            # 生成的信号文件
│   └── results/               # 回测结果（含杠杆分析）
├── growth_value_rotation_b5u_932304_mtv/  # V113 B5U-932304-MTV（年化59.44%）
│   ├── params.py              # 参数配置（3资产：932304/480080/480081）
│   ├── signal_generator_v113.py  # V113信号生成器（B5U+L5保护）
│   ├── strategy_shell.py      # 策略壳代码
│   ├── run_backtest.py        # 一键回测
│   ├── run_daily.py           # 每日执行脚本
│   ├── df_factors/            # 生成的信号文件
│   └── results/               # 回测结果
├── growth_value_rotation_b4v5/  # B4V5（+第10层反打脸保护 AW·slow_dc，年化45.31%）
│   ├── params.py              # 参数配置（含第10层 AW）
│   ├── signal_generator_dct.py# 信号生成器（10层，含 AW slow_dc）
│   ├── strategy_shell.py      # 策略壳代码
│   ├── run_backtest.py        # 一键回测
│   └── results/               # 回测结果（AW 消融明细）
├── growth_value_rotation_b4v6/  # B4V6（+第11层单腿急跌·轮动减仓 RCD，年化45.48%）
│   ├── params.py              # 参数配置（含第11层 RCD，默认 rot_z/dz1.6/scale0.5）
│   ├── signal_generator_dct.py# 信号生成器（11层，含 RCD）
│   ├── strategy_shell.py      # 策略壳代码
│   ├── run_backtest.py        # 一键回测
│   ├── run_ablation*.py       # RCD 四轮消融脚本（官方引擎）
│   ├── check_layer11*.py      # 第11层触发足迹自检
│   ├── B4V6_第11层RCD设计与消融报告.html  # ★ 设计与消融报告
│   └── results/               # 回测结果（含 rcd_ablation_engine*.csv / nav_*.csv）
├── growth_value_rotation_b4v7/  # B4V7（第12层 L5R 已撤回/默认关闭；第13层 RRS 已证伪；默认回退 B4V6 基线）
│   ├── params.py              # 参数配置（l5_repair_mode='none' 停用L5R；dr_mode='rot_z' 恢复RCD）
│   ├── signal_generator_dct.py# 信号生成器（13层，含 L5R 与 RRS 代码，默认不生效）
│   ├── strategy_shell.py      # 策略壳代码
│   ├── run_backtest.py        # 一键回测
│   ├── run_ablation*.py       # L5R 三轮消融脚本（官方引擎）
│   ├── run_rrs_ablation.py    # 第13层 RRS 消融（已证伪，劣于基线）
│   ├── crash_generality*.py   # 突发大跌"事前可规避性"检验
│   ├── check_l5r.py / check_dd.py  # 第12层触发足迹自检
│   ├── diag_whipsaw_episodes.py  # 反复打脸(震荡态)历史事件统计
│   ├── analyze_whipsaw_cases.py  # 打脸事件逐次拆解(持有段/错腿率/前兆特征/换向命中率)
│   ├── results/l5r_design_report.md  # L5R 设计与消融报告（已撤回；含RRS证伪）
│   ├── results/               # 回测结果（含 l5r_*_engine.csv / rrs_ablation_engine.csv / whipsaw_episodes.csv）
│   └── test_danger_identify.py / test_danger_actual.py  # "动量不可用"状态事前可识别性检验（前向收益/夏普）
├── growth_value_rotation_b4v8/  # B4V8（+第14层震荡不明朗·半仓 CHOP，已证伪/默认关闭；默认回退 B4V7 基线）
│   ├── params.py              # 参数配置（chop_mode='none' 默认停用；第14层参数注释）
│   ├── signal_generator_dct.py# 信号生成器（14层，含 CHOP 代码，默认不生效）
│   ├── strategy_shell.py      # 策略壳代码
│   ├── run_backtest.py        # 一键回测
│   ├── run_ablation_chop.py   # 第14层 CHOP 官方引擎消融（9变体，全部劣于基线）
│   ├── verify_zero_change.py  # 零改动验证（chop_mode='none' 与 B4V7 逐字节一致）
│   ├── diag_chop_coverage.py  # CHOP 态覆盖率/年度分布诊断（5.3%，2026窗口命中21/46天）
│   ├── results/chop_design_report.md  # ★ CHOP 设计与消融报告（已证伪，不启用）
│   └── results/               # 回测结果（chop_ablation_engine.csv / nav_chop_*.csv）
├── growth_value_rotation_b4v9/  # ★ B4V9（现行生产；B4V8基线 + aw_flip_window 20→35）
│   ├── params.py              # 参数配置（唯一差异 aw_flip_window=35；end_date=20261106）
│   ├── signal_generator_dct.py# 信号生成器（与 B4V8 同逻辑，震荡窗口35）
│   ├── strategy_shell.py      # PandaAI 策略壳代码
│   ├── run_backtest.py        # 一键引擎回测（输出 b4v9_nav.csv）
│   └── verify_signal_page.py  # ★ 线上信号页对拍（页面 generate.py vs 本目录，逐日零差异）
```

新建策略时，在 `strategies/` 下创建新的子目录，参考 `growth_value_rotation/` 的文件结构，并编写对应的 `PROJECT_GUIDE.md`。

## 环境

- **Python 环境**: conda 环境 `pandaai`
- **MongoDB**: 默认连接 `127.0.0.1:27018`，认证 `panda/panda`
- **项目根目录**: `c:\XuanJLH\pandaai\panda_quantflow\`
- **指数数据目录**: `c:\temp_v72_data\`（CSV文件，价格已÷1000缩放）

## 启动命令

```powershell
# === 策略回测 ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation
python run_backtest.py --variant baseline   # 基线B1回测（含数据预检）
python run_backtest.py --variant dir_lock_20d  # 止损变体回测

# === B5U-932304 杠杆策略（年化96.77%） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b5u_932304
python run_backtest.py                      # 一键回测（含杠杆分析）
python run_daily.py                         # 每日信号生成

# === V113 B5U-932304-MTV（年化59.44%+L5保护） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b5u_932304_mtv
python signal_generator_v113.py             # 生成V113信号
python run_backtest.py                      # 一键引擎回测

# === B4V6 成长价值轮动（+第11层单腿急跌·轮动减仓 RCD，年化45.48%） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v6
python run_backtest.py                      # 一键引擎回测（默认启用 RCD: rot_z/dz1.6/scale0.5）
python check_layer11b.py                    # 查看第11层触发足迹（dr_mode='none' 还原 B4V5 基线）

# === B4V7 成长价值轮动（第12层 L5R 已撤回/默认关闭，默认回退 B4V6 基线） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v7
python run_backtest.py                      # 一键引擎回测（默认: l5_repair_mode='none' + dr_mode='rot_z'）
python check_l5r.py                         # 查看第12层触发足迹（默认停用；设 l5_repair_mode='dd' 可复现）

# === B4V8 成长价值轮动（第14层震荡不明朗·半仓 CHOP 已证伪/默认关闭，默认回退 B4V7 基线） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v8
python run_backtest.py                      # 一键引擎回测（默认: chop_mode='none'，与 B4V7 逐字节一致）
python run_ablation_chop.py                 # 第14层 CHOP 官方引擎消融（9变体，全部劣于基线，不启用）
python verify_zero_change.py                # 零改动验证
python sweep_aw_params.py                   # AW 阶段1：flip_window×flip_thresh×dc_strict 网格
python sweep_aw_modes.py                    # AW 阶段2：结构变体（anchor/graded/freeze/tgate）
python sweep_aw_phase3.py                   # AW 阶段3：稳健性验证与机制归因
python sweep_aw_phase4.py                   # AW 阶段4：窗口边界扫描（fw 上界，fw=35 Calmar 最优）
python sweep_aw_phase5.py                   # AW 阶段5：确认时长/窗口继续放大（均劣化）
python sweep_aw_asym.py                     # AW 阶段6：非对称确认 A-D 组（震荡态内/全期/反向对照）
python sweep_aw_asym2.py                    # AW 阶段6·补：非对称确认 E 组（反向保持 7）
python check_asym_equiv2.py                 # 非对称实现自检（锚点等价性 + 持仓腿差异）
python diag_mdd_window.py                   # 最大回撤区间定位（各配置均落在 2018-02-02~2019-01-03）

# === B4V9 成长价值轮动（现行生产；B4V8基线 + aw_flip_window 20→35） ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation_b4v9
python run_backtest.py                      # 一键引擎回测（aw_mode=slow_dc/fw35 + dr_mode=rot_z）
python verify_signal_page.py                # 线上信号页对拍（fw35生产 + fw20回归，两组须全 PASS）

# === 数据更新 ===
cd c:\XuanJLH\pandaai\strategies\growth_value_rotation
python update_data.py                       # 更新ETF数据到最新日期（CSV=指数点；Mongo 引擎库统一 /1000 量纲）
python restore_index_history.py             # 紧急重建：以 CSV 为真值全量重建 Mongo 行情（统一 /1000 量纲）
python precheck.py                          # 仅数据预检

# === 每日信号 ===
cd c:\XuanJLH\pandaai\panda_quantflow
python ..\signal_page\generate.py           # 生成 B1 每日信号

# === B4V9 线上信号页（GitHub Actions 自动发布 GitHub Pages） ===
# 工作流: .github/workflows/daily_signal.yml（北京时间工作日21:00，也可 workflow_dispatch 手动触发）
# 入口: signal_page_b4/generate.py（自包含，cnindex官网数据 + adata ETF兜底；缓存 signal_page_b4/data）
# 页面: signal_page_b4/docs/index.html；JSON: signal_page_b4/docs/signal.json
cd c:\XuanJLH\pandaai
python signal_page_b4\generate.py           # 本地等价运行（默认输出 signal_page_b4/docs）
```
