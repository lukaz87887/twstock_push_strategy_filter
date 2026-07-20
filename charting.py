# -*- coding: utf-8 -*-
"""
charting.py — K 線技術圖 (headless) + 6 格拼圖

繪圖器 CandlestickPlotter 由桌面版原樣移植 (紅漲綠跌 + 多均線 + 量能副圖 +
通道壓力線 / 抗跌星號 等策略標記)。

對外主要函式:
  • render_stock_png(df, title, extra)  → 單檔 K 線技術圖的 PNG bytes
  • montage(png_list, cols=2)           → 把多張 PNG 併成一張 (最多 6 格) 的 PNG bytes
"""
import io
import platform

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")            # ★ headless: 無視窗後端
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from matplotlib import font_manager
import mplfinance as mpf

from PIL import Image


def setup_chinese_font():
    """依 OS 自動選用支援中文的字型。"""
    system_name = platform.system()
    candidates = {
        "Windows": ["Microsoft JhengHei", "Microsoft YaHei", "PMingLiU", "SimHei"],
        "Darwin":  ["PingFang TC", "Heiti TC", "Arial Unicode MS", "STHeiti"],
        "Linux":   ["Noto Sans CJK TC", "Noto Sans TC", "WenQuanYi Zen Hei",
                    "WenQuanYi Micro Hei", "AR PL UMing TW", "Droid Sans Fallback"],
    }.get(system_name, [])
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = next((n for n in candidates if n in available), None)

    if chosen:
        plt.rcParams["font.sans-serif"] = [chosen]
        plt.rcParams["axes.unicode_minus"] = False
        print(f"[INFO] 已設定中文字型: {chosen}")
    else:
        print("[WARN] 未偵測到中文字型, 中文可能顯示為方框。")
    return chosen



CHINESE_FONT = setup_chinese_font()


class CandlestickPlotter:
    """繪製專業 K 線圖 (台股紅漲綠跌 + 三均線 + 量能副圖)"""

    @staticmethod
    def get_taiwan_style() -> dict:
        market_colors = mpf.make_marketcolors(
            up="red", down="green", edge="inherit",
            wick={"up": "red", "down": "green"},
            volume={"up": "red", "down": "green"},
        )
        return mpf.make_mpf_style(
            base_mpf_style="yahoo",
            marketcolors=market_colors,
            gridstyle=":", gridcolor="#D0D0D0",
            facecolor="white", edgecolor="#888888",
            rc={"font.sans-serif": [CHINESE_FONT] if CHINESE_FONT else ["sans-serif"],
                "axes.unicode_minus": False},
        )

    @classmethod
    def plot(cls, fig: Figure, df: pd.DataFrame, title: str,
             extra: dict = None, simple_mode: bool = False,
             inst_series: pd.DataFrame = None):
        """
        在 fig 上繪製: K 線 + (可選)投信買賣量 + 量能
        extra : 額外資訊, 用於繪製策略特定的標記
            - {"pattern":"channel_breakout", ...}
            - {"rs_count", "rs_dates", "latest_crash"}
            - {"disposal_start", "disposal_end"}
        simple_mode : v9.3.4 - 簡化模式
            - False (預設): 5/20/30/45/60 五均線 + 圖例
            - True : 只畫 20MA 月線 + 不畫圖例
        inst_series : v9.4 新增 - 投信買賣超時序
            - None: 兩圖佈局 (K 線 + 量能), 跟以前一樣
            - DataFrame[date, sit_net_lots]: 三圖佈局 (K 線 + 投信 + 量能)
              紅 bar = 買超, 綠 bar = 賣超
        """
        fig.clear()
        plot_df = df.tail(120).copy()

        if len(plot_df) < 10:
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "資料不足, 無法繪圖", ha="center", va="center",
                    fontsize=14, transform=ax.transAxes)
            ax.axis("off")
            return None, None, plot_df, None  # ★ v9.4: 4 個值

        # 均線
        plot_df["MA20"] = df["Close"].rolling(20).mean().loc[plot_df.index]
        if not simple_mode:
            plot_df["MA5"]  = df["Close"].rolling(5).mean().loc[plot_df.index]
            plot_df["MA30"] = df["Close"].rolling(30).mean().loc[plot_df.index]
            plot_df["MA45"] = df["Close"].rolling(45).mean().loc[plot_df.index]
            plot_df["MA60"] = df["Close"].rolling(60).mean().loc[plot_df.index]

        # ★ v9.4: 三圖佈局 (K 線 + 投信 + 量能) 或兩圖 (K 線 + 量能)
        ax_inst = None
        if inst_series is not None and not inst_series.empty:
            # 三圖: K(6) + 投信(2) + 量能(2) = 10 列
            gs = fig.add_gridspec(10, 1, hspace=0.08)
            ax_main = fig.add_subplot(gs[0:6, 0])
            ax_inst = fig.add_subplot(gs[6:8, 0], sharex=ax_main)
            ax_vol  = fig.add_subplot(gs[8:10, 0], sharex=ax_main)
        else:
            # 兩圖 (跟以前一樣): K(3/4) + 量(1/4)
            ax_main = fig.add_subplot(4, 1, (1, 3))
            ax_vol  = fig.add_subplot(4, 1, 4, sharex=ax_main)

        # 依模式準備 addplot 清單
        if simple_mode:
            add_plots = [
                mpf.make_addplot(plot_df["MA20"], ax=ax_main,
                                 color="#1E90FF", width=2.0),
            ]
        else:
            add_plots = [
                mpf.make_addplot(plot_df["MA5"],  ax=ax_main, color="#FF8C00", width=1.2),
                mpf.make_addplot(plot_df["MA20"], ax=ax_main, color="#1E90FF", width=1.2),
                mpf.make_addplot(plot_df["MA30"], ax=ax_main, color="#00B894", width=1.2),
                mpf.make_addplot(plot_df["MA45"], ax=ax_main, color="#E91E63", width=1.2),
                mpf.make_addplot(plot_df["MA60"], ax=ax_main, color="#8B008B", width=1.4),
            ]

        try:
            mpf.plot(
                plot_df, type="candle",
                ax=ax_main, volume=ax_vol, addplot=add_plots,
                style=cls.get_taiwan_style(),
                datetime_format="%m/%d", xrotation=0,
                show_nontrading=False, warn_too_much_data=10000,
            )
        except Exception as e:
            ax_main.text(0.5, 0.5, f"繪圖錯誤: {e}", ha="center", va="center",
                         transform=ax_main.transAxes)

        # ---- 標題與軸標籤 ----
        ax_main.set_title(title, fontsize=14, fontweight="bold", pad=10)
        ax_main.set_ylabel("價格 (TWD)", fontsize=10)

        # ---- 修正量能軸顯示單位 ----
        ax_vol.set_ylabel("成交量 (張)", fontsize=10)
        ax_vol.yaxis.set_major_formatter(
            FuncFormatter(lambda x, _: f"{int(x / 1000):,}" if x >= 0 else "")
        )

        # ---- 均線圖例 ----
        # ★ v9.3.4: simple_mode 不畫圖例 (避免和 OHLC 浮動框搶左上角)
        if not simple_mode:
            from matplotlib.lines import Line2D
            legend_lines = [
                Line2D([0], [0], color="#FF8C00", lw=2, label="5MA"),
                Line2D([0], [0], color="#1E90FF", lw=2, label="20MA (月線)"),
                Line2D([0], [0], color="#00B894", lw=2, label="30MA"),
                Line2D([0], [0], color="#E91E63", lw=2, label="45MA"),
                Line2D([0], [0], color="#8B008B", lw=2.5, label="60MA (季線)"),
            ]
            ax_main.legend(handles=legend_lines, loc="upper left",
                           fontsize=8, framealpha=0.9, ncol=2)

        # ====== ★ v9.4: 投信買賣超副圖 ======
        if ax_inst is not None and inst_series is not None and not inst_series.empty:
            try:
                # 建立 plot_df.index → 在 inst_series 找對應日期 的 sit_net_lots
                # plot_df.index 是 DatetimeIndex, mpf 用 bar index 當 x
                # 所以我們要把 sit_net_lots 對齊到每根 K 棒的 bar index
                inst_lookup = {}
                for _, row in inst_series.iterrows():
                    inst_lookup[pd.Timestamp(row['date']).normalize()] = int(row['sit_net_lots'])

                bar_indices = []
                values = []
                for i, ts in enumerate(plot_df.index):
                    ts_norm = pd.Timestamp(ts).normalize()
                    if ts_norm in inst_lookup:
                        bar_indices.append(i)
                        values.append(inst_lookup[ts_norm])

                if bar_indices:
                    # 紅 bar = 買超 (正), 綠 bar = 賣超 (負)
                    colors = ["#C62828" if v > 0 else "#2E7D32" if v < 0 else "#888"
                              for v in values]
                    ax_inst.bar(bar_indices, values, color=colors,
                                width=0.7, edgecolor="none")
                    # 0 水平線
                    ax_inst.axhline(y=0, color="#444", linewidth=0.6,
                                    linestyle="-", alpha=0.7)

                ax_inst.set_ylabel("投信買賣超\n(張)", fontsize=9)
                ax_inst.tick_params(axis='y', labelsize=8)
                # 不要顯示 x 軸標籤 (跟 ax_main 共用)
                ax_inst.tick_params(axis='x', labelbottom=False)
                ax_inst.grid(True, linestyle=":", alpha=0.3)
                ax_inst.yaxis.set_major_formatter(
                    FuncFormatter(lambda x, _: f"{int(x):,}")
                )
                # 對稱 Y 軸範圍 (買超與賣超對等)
                if values:
                    abs_max = max(abs(min(values)), abs(max(values)))
                    if abs_max > 0:
                        ax_inst.set_ylim(-abs_max * 1.15, abs_max * 1.15)
            except Exception as e:
                print(f"[WARN] 投信副圖繪製失敗: {e}")

        # ====== v7 新增: 策略特定標記 ======
        if extra:
            cls._draw_extra_markers(ax_main, df, plot_df, extra)

        # ====== ★ v9.3.7 動態 Y 軸範圍 (避免 K 線擠在上半部) ======
        # 主圖: 用 High/Low 真實範圍 + padding (上方多留給 OHLC 浮動框)
        try:
            price_low  = float(plot_df["Low"].min())
            price_high = float(plot_df["High"].max())
            price_range = price_high - price_low

            if price_range > 0:
                # ★ v9.3.8: 上方留 12% (足夠 OHLC 浮動框 + 處置標籤)
                # 下方留 5% 緩衝
                y_top = price_high + price_range * 0.12
                y_bottom = max(0, price_low - price_range * 0.05)
                ax_main.set_ylim(y_bottom, y_top)

            # 量能副圖: 上方留 8% padding (預設下方就是 0)
            vol_max = float(plot_df["Volume"].max())
            if vol_max > 0:
                ax_vol.set_ylim(0, vol_max * 1.15)
        except Exception as e:
            print(f"[WARN] 設定 Y 軸範圍失敗: {e}")

        fig.tight_layout()
        # ★ v9.4: return 加上 ax_inst (None 表示沒有投信副圖)
        return ax_main, ax_vol, plot_df, ax_inst

    @classmethod
    def _draw_extra_markers(cls, ax_main, df_full, plot_df, extra):
        """
        在主圖上繪製策略特定的視覺元素:
        - 通道突破: 紫色虛線壓力線 + 旗桿頂端三角
        - RS Strong: 大盤大跌日股票收紅的星號標記
        """
        from matplotlib.lines import Line2D
        pattern = extra.get("pattern")

        # ---- 通道突破: 畫下降壓力線 ----
        if pattern == "channel_breakout":
            slope = extra.get("slope")
            intercept = extra.get("intercept")
            peak_global_idx = extra.get("peak_global_idx")
            channel_days = extra.get("channel_days", 0)

            if slope is None or peak_global_idx is None:
                return

            # 旗桿頂端在完整 df 的日期
            if peak_global_idx >= len(df_full):
                return
            peak_date = df_full.index[peak_global_idx]

            # 壓力線繪製範圍: 從旗桿頂端到 plot_df 結束 (或今天前一天)
            # mplfinance 用「索引位置」當 x, 我們需要找到 plot_df 對應的 x 座標
            try:
                # plot_df 內若有 peak_date
                if peak_date in plot_df.index:
                    plot_peak_pos = plot_df.index.get_loc(peak_date)
                else:
                    # peak 在 plot 範圍外, 從左端開始繪線
                    plot_peak_pos = 0
            except KeyError:
                plot_peak_pos = 0

            # 對應壓力線 y 值: y = slope*x + intercept, x 從 0 算起 (correction 內)
            # 但 plot 的 x 軸是 mplfinance 的索引位置, 我們重建之
            plot_end_pos = len(plot_df) - 1
            x_plot_range = np.arange(plot_peak_pos, plot_end_pos + 1)
            # 對應到 correction 內的 x (peak 在 correction 第 0 個點)
            x_in_correction = x_plot_range - plot_peak_pos
            y_resistance = slope * x_in_correction + intercept

            ax_main.plot(x_plot_range, y_resistance,
                         color="magenta", linewidth=2.2, linestyle="--",
                         label=f"通道壓力線 ({channel_days}日)",
                         zorder=5)

            # 標記旗桿頂端
            try:
                peak_high = float(df_full.loc[peak_date, "High"])
                ax_main.scatter([plot_peak_pos], [peak_high],
                                color="black", s=120, marker="v",
                                label="旗桿頂端", zorder=6)
            except Exception:
                pass

            # 更新圖例 (加入新項目)
            handles, labels = ax_main.get_legend_handles_labels()
            ax_main.legend(handles=handles, loc="upper left",
                           fontsize=8, framealpha=0.9)

        # ---- RS Strong: 標記抗跌日 ----
        rs_dates = extra.get("rs_dates")
        latest_crash = extra.get("latest_crash")
        if rs_dates:
            normal_pts_x, normal_pts_y = [], []
            gold_pts_x, gold_pts_y = [], []

            for d_str in rs_dates:
                d = pd.to_datetime(d_str)
                if d not in plot_df.index:
                    continue
                try:
                    pos = plot_df.index.get_loc(d)
                    low_val = float(plot_df.loc[d, "Low"])
                    # 在 K 棒下方偏移一點點
                    y = low_val * 0.98
                    if d_str == latest_crash:
                        gold_pts_x.append(pos)
                        gold_pts_y.append(y)
                    else:
                        normal_pts_x.append(pos)
                        normal_pts_y.append(y)
                except (KeyError, ValueError):
                    continue

            if normal_pts_x:
                ax_main.scatter(normal_pts_x, normal_pts_y,
                                color="magenta", s=120, marker="^",
                                label="大盤跌日抗跌", zorder=5)
            if gold_pts_x:
                ax_main.scatter(gold_pts_x, gold_pts_y,
                                color="gold", s=220, marker="*",
                                edgecolors="black", linewidths=1.5,
                                label="最新大跌日抗跌", zorder=6)

            handles, labels = ax_main.get_legend_handles_labels()
            ax_main.legend(handles=handles, loc="upper left",
                           fontsize=8, framealpha=0.9)

        # ---- v9.3.1: 處置期間色帶 ----
        disposal_start = extra.get("disposal_start")
        disposal_end   = extra.get("disposal_end")
        if disposal_start and disposal_end:
            try:
                ds = pd.to_datetime(disposal_start)
                de = pd.to_datetime(disposal_end)
                # 找出對應 plot_df 的 index 位置 (找最近的交易日)
                # plot_df.index 是 DatetimeIndex
                # 在期間內的交易日
                in_period_mask = (plot_df.index >= ds) & (plot_df.index <= de)
                in_period_positions = np.where(in_period_mask)[0]
                if len(in_period_positions) > 0:
                    start_pos = in_period_positions[0] - 0.5
                    end_pos   = in_period_positions[-1] + 0.5
                    ax_main.axvspan(start_pos, end_pos,
                                    color="#FF6B6B", alpha=0.13,
                                    label="處置期間", zorder=1)

                    # ★ v9.3.8: 標籤改放在 axes 頂部「絕對位置」
                    #   - x 用資料座標 (色帶中心)
                    #   - y 用 axes 座標 (0.95 = 距頂 5%)
                    #   → 不會蓋到 K 線, 永遠在頂部
                    mid_pos = (start_pos + end_pos) / 2
                    n_bars = len(plot_df)

                    # 標籤要靠右 / 居中 / 靠左, 根據色帶位置動態決定
                    if mid_pos > n_bars * 0.85:
                        # 色帶在最右邊 (常見, 因為處置期間多半是現在進行式)
                        # → 標籤放在「色帶左邊」, 避免跑出圖外
                        label_x = max(start_pos - 1, n_bars * 0.7)
                        ha = "right"
                    elif mid_pos < n_bars * 0.15:
                        label_x = end_pos + 1
                        ha = "left"
                    else:
                        label_x = mid_pos
                        ha = "center"

                    # 使用 axes 的 blended transform: x=data, y=axes
                    from matplotlib import transforms as _t
                    trans = _t.blended_transform_factory(
                        ax_main.transData, ax_main.transAxes)
                    ax_main.text(label_x, 0.95, "🚨 處置期間",
                                 transform=trans,
                                 ha=ha, va="top", fontsize=9,
                                 color="#C62828", fontweight="bold",
                                 clip_on=False,
                                 zorder=15,
                                 bbox=dict(boxstyle="round,pad=0.3",
                                           facecolor="#FFE0E0",
                                           edgecolor="#C62828",
                                           alpha=0.92))
            except Exception as e:
                print(f"[WARN] 處置期間標示失敗: {e}")



# ==================================================================
#   headless 輸出: 單檔 PNG + 6 格拼圖
# ==================================================================
def render_stock_png(df: pd.DataFrame, title: str, extra: dict = None,
                     width_px: int = 900, height_px: int = 720,
                     dpi: int = 100) -> bytes | None:
    """把單一檔股票畫成 K 線技術圖, 回傳 PNG bytes。"""
    if df is None or df.empty:
        return None
    fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    try:
        CandlestickPlotter.plot(fig, df, title, extra=extra, simple_mode=False)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                    facecolor="white")
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"[chart] 繪圖失敗 {title}: {e}")
        return None
    finally:
        plt.close(fig)


def montage(png_list: list, cols: int = 2, gap: int = 12,
            bg=(255, 255, 255)) -> bytes | None:
    """把多張 PNG (最多建議 6 張) 併成一張大圖 (grid)。回傳 PNG bytes。"""
    imgs = [Image.open(io.BytesIO(p)).convert("RGB") for p in png_list if p]
    if not imgs:
        return None
    n = len(imgs)
    cols = min(cols, n)
    rows = (n + cols - 1) // cols
    # 以最大格為基準, 每格置中貼上
    cell_w = max(im.width for im in imgs)
    cell_h = max(im.height for im in imgs)
    board_w = cols * cell_w + (cols + 1) * gap
    board_h = rows * cell_h + (rows + 1) * gap
    board = Image.new("RGB", (board_w, board_h), bg)
    for idx, im in enumerate(imgs):
        r, c = divmod(idx, cols)
        x = gap + c * (cell_w + gap) + (cell_w - im.width) // 2
        y = gap + r * (cell_h + gap) + (cell_h - im.height) // 2
        board.paste(im, (x, y))
    out = io.BytesIO()
    board.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()


def chunk(seq: list, size: int = 6):
    """把清單切成每 size 一組 (最後一組可不足)。"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
