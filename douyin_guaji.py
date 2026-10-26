"""抖音福袋挂机入口

默认会开一个半透明置顶悬浮窗，显示开奖倒计时与实时运行日志；
没有图形环境（服务器/SSH）或 Python 没带 tkinter 时会自动退回纯控制台运行，
也可以用 --no-gui 强制关闭悬浮窗。

线程模型：tkinter 必须在主线程跑 mainloop，所以挂机逻辑放在后台守护线程，
两边只通过 fudai_status 里带锁的状态对象通信，互不直接调用。
"""

import argparse
import sys
import threading

from douyin_fudai import fudai_analyse
from fudai_overlay import create_overlay


class fudai_guaji:
    def __init__(self):
        self.analyser = fudai_analyse()

    def guaji(self):
        """挂机函数：未检测到设备时持续重试（循环实现，避免递归耗尽调用栈）"""
        while True:
            device_id = self.analyser.operation.select_device()
            if device_id:
                self.analyser.fudai_choujiang(device_id, False, 15)
                return True
            self.analyser.operation.delay(10)


def main(argv=None):
    parser = argparse.ArgumentParser(description='抖音福袋挂机')
    parser.add_argument('--no-gui', action='store_true',
                        help='不显示悬浮窗，仅用控制台输出')
    args = parser.parse_args(argv)

    choujiang = fudai_guaji()
    analyser = choujiang.analyser

    overlay = None
    if analyser.overlay_enabled and not args.no_gui:
        overlay = create_overlay(analyser.status, analyser.log_bus,
                                 alpha=analyser.overlay_alpha_percent / 100.0,
                                 log_lines=analyser.overlay_log_lines)

    if overlay is None:
        # 没有悬浮窗就按老方式在主线程直接跑
        choujiang.guaji()
        return 0

    worker = threading.Thread(target=choujiang.guaji, name='fudai-worker', daemon=True)
    worker.start()
    print("悬浮窗已启动：按住任意位置拖动，点右上角 ✕ 或右键关闭（关闭即退出程序）")
    try:
        overlay.run()
    finally:
        print("悬浮窗已关闭，程序退出")
    return 0


if __name__ == '__main__':
    sys.exit(main())
