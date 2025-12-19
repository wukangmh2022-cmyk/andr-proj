import numpy as np
import matplotlib.pyplot as plt

# ====== 输入原始数据 ======
data_str = """
-3.76%
-0.97%
1.35%
-3.16%
1.92%
-2.49%
-3.00%
-2.87%
0.54%
0.22%
-2.01%
-4.91%
4.01%
-2.45%
-1.85%
14.25%
1.34%
-2.33%
-1.10%
-2.65%
-3.08%
3.18%
-4.19%
2.72%
14.28%
6.14%
2.55%
-4.30%
4.87%
-2.76%
-2.47%
-4.58%
-0.63%
-1.48%
10.88%
-4.31%
-4.03%
-0.99%
-1.00%
-0.85%
-0.47%
8.65%
0.53%
3.75%
-3.30%
-4.38%
3.17%
7.39%
-0.65%
1.47%
-0.07%
-1.73%
1.43%
-2.99%
-3.92%
-1.25%
6.74%
3.23%
-3.17%
-1.18%
-1.35%
-2.56%
-0.13%
-4.60%
12.57%
-1.93%
12.19%
16.59%
-2.07%
-2.68%
-0.71%
-2.41%
-2.58%
-1.70%
-1.88%
13.50%
-2.60%
-4.76%
8.16%
8.21%
-2.37%
-9.97%
13.05%
0.35%
-1.76%
5.20%
-1.24%
-0.66%
0.79%
17.33%
-2.59%
8.28%
-1.98%
-0.94%
4.51%
5.04%
-2.47%
-0.79%
1.09%
7.45%
-1.68%
8.94%
13.05%
"""
# -6.07%
# -1.66%
# 13.28%
# -1.03%
# -1.47%
# -2.32%
# -1.71%
# 1.63%
# -0.83%
# 2.03%
# 7.89%
# 24.26%
# 0.00%
# -0.79%
# -2.10%
# -1.19%
# -1.29%
# 5.95%
# -0.78%
# -1.73%
# 1.57%
# -5.25%
# -2.64%
# -2.04%
# -1.82%
# -2.07%
# 14.73%
# -1.73%
# -2.17%
# 0.91%
# -1.76%
# -2.74%
# -1.66%
# -3.36%
# -1.66%
# 5.64%
# -1.42%
# -1.17%
# -1.38%
# 2.17%
# 1.77%
# 1.88%
# 0.54%
# -1.69%
# 0.65%
# -2.84%
# -0.74%
# 3.64%
# 3.65%
# -2.28%
# 1.49%
# -5.09%
# -5.19%
# 17.57%
# 26.13%
# -5.16%
# -5.46%
# -3.42%
# 20.54%
# 13.77%
# 34.85%
# -2.33%
# -3.76%
# -4.99%
# -10.20%
# 13.77%
# 13.04%
# -3.27%
# 12.79%
# 13.50%
# -5.47%
# -6.38%
# -2.87%
# -1.03%
# -7.20%
# -5.25%
# -0.63%
# -1.75%
# -2.35%
# 12.86%
# -2.37%
# -2.86%
# -0.12%
# -5.30%
# -2.04%
# -2.91%
# -2.60%
# -4.17%
# -2.45%
# -3.30%
# 5.78%
# 4.52%
# -4.05%
# -2.78%
# -1.42%
# 2.54%
# -2.34%
# -2.27%
# 2.93%
# -3.65%
# 5.05%
# 2.74%
# 2.38%
# 0.60%
# 17.81%
# 5.96%
# 14.43%
# -1.63%
# -6.05%
# """

# 转换为浮点数组
returns = np.array([float(x.strip().replace('%', ''))/100 for x in data_str.splitlines() if x.strip()])

# ====== 计算函数 ======
def calc_curves(returns, leverage=1):
    r = returns * leverage
    # 非复利：逐步累加（百分比）
    equity_add = np.cumsum(r * 100)
    # 复利：连乘
    equity_comp = np.cumprod(1 + r)
    return equity_add, equity_comp

# 计算最大回撤
def max_drawdown(series):
    peak = np.maximum.accumulate(series)
    dd = series - peak
    return dd.min()  # 返回负值

# 杠杆倍数列表
leverage_list = [1, 2, 3, 4]
results = {}

for lev in leverage_list:
    results[lev] = calc_curves(returns, lev)

# ====== 绘图 ======
plt.figure(figsize=(14,6))  # 窗体矮一些，左右布局

# 左图：非复利
plt.subplot(1,2,1)
for lev in leverage_list:
    plt.plot(results[lev][0], label=f"{lev}x")
plt.title("Non-Compounding Cumulative Returns (1x~4x)")
plt.xlabel("Index")
plt.ylabel("Cumulative Return (%)")
plt.legend()
plt.grid(True)

# 右图：复利
plt.subplot(1,2,2)
for lev in leverage_list:
    plt.plot(results[lev][1], label=f"{lev}x")
plt.title("Compounded Equity Growth (1x~4x)")
plt.xlabel("Index")
plt.ylabel("Equity (Growth Factor)")
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()

# ====== 输出最终结果 ======
for lev in leverage_list:
    equity_add, equity_comp = results[lev]
    maxdd_add = max_drawdown(equity_add)
    maxdd_comp = max_drawdown(equity_comp)
    print(f"{lev}x 杠杆:")
    print(f"  最终累计收益 (非复利): {equity_add[-1]:.2f}%")
    print(f"  最终权益倍数 (复利): {equity_comp[-1]:.2f}x")
    print(f"  最大回撤 (非复利): {maxdd_add:.2f}%")
    print(f"  最大回撤 (复利): {maxdd_comp*100:.2f}%")
    print()
