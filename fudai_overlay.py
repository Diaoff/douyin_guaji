"""半透明置顶悬浮窗：显示开奖倒计时与实时运行日志。

设计要点
--------
1. **tkinter 必须在主线程跑 mainloop**，所以自动化逻辑放到后台线程，
   见 douyin_guaji.py 的 main()。两边只通过 fudai_status 通信。
2. **没有图形环境（服务器/SSH）或 Python 没带 tkinter 时必须安全降级**：
   create_overlay() 返回 None，程序照常以纯控制台方式运行 —— 挂机不能因为
   一个窗口起不来就罢工。
3. 整窗半透明用 -alpha；可读性靠「深色底 + 浅色字 + 0.78 左右不透明度」保证。
   注意 -alpha 会让**文字也一起变淡**，alpha 调太低会看不清。
4. 窗口无边框（overrideredirect），所以自己实现了拖动和关闭：
   按住任意位置拖动，点右上角 ✕ 或右键关闭（关闭即退出程序）。
"""

import sys

try:
    import tkinter as tk
except ImportError:  # 极少数精简版 Python 不带 tkinter
    tk = None

from fudai_status import format_countdown, format_snapshot_lines

BG = '#101418'
FG = '#e8f0f5'
FG_DIM = '#8fa3b0'
ACCENT = '#7fd1ff'
LOG_FG = '#b9c7d1'
UI_FONT = 'Microsoft YaHei UI'
MONO_FONT = 'Consolas'


class OverlayWindow:
    """悬浮窗；构造即显示，run() 进入事件循环"""

    def __init__(self, status, log_bus, alpha=0.78, width=470, height=330,
                 log_lines=10, poll_ms=300, title='抖音福袋挂机 · 监控'):
        if tk is None:
            raise RuntimeError('当前 Python 环境没有 tkinter')
        self.status = status
        self.log_bus = log_bus
        self.poll_ms = max(50, int(poll_ms))
        self.log_lines = max(1, int(log_lines))
        self._closed = False
        self._drag_offset = (0, 0)

        self.root = tk.Tk()
        self.root.title(title)
        self.root.overrideredirect(True)          # 去掉标题栏与边框
        self.root.attributes('-topmost', True)    # 置顶
        try:
            self.root.attributes('-alpha', float(alpha))   # 整窗半透明
        except tk.TclError:
            pass                                   # 个别平台不支持就算了
        self.root.configure(bg=BG)
        self.root.geometry('{}x{}+{}+{}'.format(int(width), int(height), 60, 60))

        self._build(title)
        self._bind_drag(exclude=(self.close_button,))
        self.update_once()
        self._tick()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build(self, title):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill='x', padx=12, pady=(9, 0))
        tk.Label(header, text=title, bg=BG, fg=FG,
                 font=(UI_FONT, 10, 'bold')).pack(side='left')
        self.close_button = tk.Label(header, text='✕', bg=BG, fg=FG_DIM,
                                     font=(UI_FONT, 10, 'bold'), cursor='hand2')
        self.close_button.pack(side='right')
        self.close_button.bind('<Button-1>', lambda event: self.close())
        self.device_label = tk.Label(header, text='', bg=BG, fg=FG_DIM, font=(UI_FONT, 8))
        self.device_label.pack(side='right', padx=(0, 10))

        self.countdown_label = tk.Label(self.root, text='--:--', bg=BG, fg=ACCENT,
                                        font=(UI_FONT, 30, 'bold'), anchor='w')
        self.countdown_label.pack(fill='x', padx=12, pady=(2, 0))

        self.info_label = tk.Label(self.root, text='', bg=BG, fg=FG, justify='left',
                                   anchor='w', font=(UI_FONT, 9))
        self.info_label.pack(fill='x', padx=12)

        tk.Label(self.root, text='运行日志', bg=BG, fg=FG_DIM, anchor='w',
                 font=(UI_FONT, 8)).pack(fill='x', padx=12, pady=(8, 0))
        # height 用「文本行数」，这样日志增减时窗口高度不会跳动
        self.log_label = tk.Label(self.root, text='', bg=BG, fg=LOG_FG, justify='left',
                                  anchor='nw', font=(MONO_FONT, 8), height=self.log_lines)
        self.log_label.pack(fill='both', expand=True, padx=12, pady=(0, 8))

    def _bind_drag(self, widget=None, exclude=()):
        """无边框窗口需要自己实现拖动；右键可关闭"""
        widget = widget or self.root
        if widget not in exclude:
            widget.bind('<ButtonPress-1>', self._start_drag)
            widget.bind('<B1-Motion>', self._on_drag)
        widget.bind('<Button-3>', lambda event: self.close())
        for child in widget.winfo_children():
            self._bind_drag(child, exclude)

    def _start_drag(self, event):
        self._drag_offset = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _on_drag(self, event):
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry('+{}+{}'.format(x, y))

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def update_once(self):
        """按当前状态刷新一次（测试里也直接调它）"""
        snapshot = self.status.snapshot()
        self.countdown_label.config(text=format_countdown(snapshot.get('remaining')))
        self.device_label.config(text=snapshot.get('device_id') or '')
        info_lines = format_snapshot_lines(snapshot, include_countdown=False)
        self.info_label.config(text='\n'.join(info_lines))
        self.log_label.config(text='\n'.join(self.log_bus.tail(self.log_lines)))

    def _tick(self):
        if self._closed:
            return
        try:
            self.update_once()
        except tk.TclError:
            return                      # 窗口已被销毁
        self.root.after(self.poll_ms, self._tick)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self):
        """进入事件循环（必须在主线程调用）"""
        self.root.mainloop()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def create_overlay(status, log_bus, **kwargs):
    """创建悬浮窗；无图形环境或缺少 tkinter 时返回 None

    这里刻意捕获所有异常：窗口起不来绝不能影响挂机主流程，
    调用方拿到 None 就退回纯控制台运行。
    """
    try:
        return OverlayWindow(status, log_bus, **kwargs)
    except Exception as e:
        print("悬浮窗创建失败({}: {})，改用纯控制台运行".format(type(e).__name__, e))
        return None


if __name__ == '__main__':
    sys.exit(0)
