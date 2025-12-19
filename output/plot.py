import pandas as pd
import matplotlib.pyplot as plt

# 为了中文不变方块（Mac 示例，如果是 Windows，可用 SimHei 或 Microsoft YaHei）
plt.rcParams['font.sans-serif'] = ['PingFang SC', 'STHeiti', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

# 读取文件
df = pd.read_csv("1.csv")

# 数值处理
df['pnl_usdt'] = df['pnl_usdt'].replace({'\$': '', ',': ''}, regex=True)
df['pnl_usdt'] = pd.to_numeric(df['pnl_usdt'], errors='coerce')

df['leverage'] = pd.to_numeric(df['leverage'], errors='coerce')

# 组合 date + time 为开仓时间
df['open_time'] = pd.to_datetime(
    df['date'].astype(str) + ' ' + df['time'].astype(str),
    errors='coerce'
)

# 按时间排序
df = df.sort_values(by='open_time').reset_index(drop=True)

# 累计收益（如果你想以“万 USDT”为单位，打开下面这一行，把 /10000 打开）
df['cum_pnl'] = df['pnl_usdt'].cumsum()
# df['cum_pnl'] = df['pnl_usdt'].cumsum() / 10000  # ← 用万 USDT 展示时用这一行

# 最大回撤计算
df['cum_pnl_peak'] = df['cum_pnl'].cummax()
df['drawdown'] = df['cum_pnl_peak'] - df['cum_pnl']
df['max_drawdown_pct'] = df['drawdown'] / df['cum_pnl_peak'].apply(lambda x: max(1, x)) * 100

# 多空拆分（目前只用于后续扩展，可以先不用）
df_long = df[df['side'] == '多']
df_short = df[df['side'] == '空']

# =========================
# 图 1：累计收益曲线
# =========================
fig1, ax1 = plt.subplots(figsize=(12, 6))
ax1.plot(df['open_time'], df['cum_pnl'], label='累计收益 (USDT)', linewidth=2)

ax1.set_title('累计收益曲线 (Cumulative P&L)', fontsize=16)
ax1.set_xlabel('开仓时间', fontsize=12)
ax1.set_ylabel('累计收益 (USDT)', fontsize=12)

# 关闭科学计数法显示
ax1.ticklabel_format(style='plain', axis='y')

ax1.grid(True, linestyle='--', alpha=0.6)
ax1.legend()
plt.tight_layout()

# =========================
# 图 2：最大回撤百分比曲线
# =========================
fig2, ax2 = plt.subplots(figsize=(12, 6))
ax2.plot(df['open_time'], df['max_drawdown_pct'], label='最大回撤率 (%)', linewidth=2)
ax2.fill_between(df['open_time'], df['max_drawdown_pct'], 0, alpha=0.3)

ax2.set_title('相对历史高点的最大回撤百分比曲线 (Maximum Drawdown %)', fontsize=16)
ax2.set_xlabel('开仓时间', fontsize=12)
ax2.set_ylabel('最大回撤率 (%)', fontsize=12)

ax2.grid(True, linestyle='--', alpha=0.6)
ax2.legend()
plt.tight_layout()

plt.show()

# =========================
# 关键统计数据
# =========================
final_cumulative_profit = df['cum_pnl'].iloc[-1]
max_drawdown = df['max_drawdown_pct'].max()

print("\n--- 关键统计数据 ---")
print(f"最终累计总收益: {final_cumulative_profit:,.2f} （单位与 cum_pnl 一致）")
print(f"历史最大回撤率: {max_drawdown:,.2f}% (相对历史高点)")
print(f"总交易笔数: {len(df)}")
