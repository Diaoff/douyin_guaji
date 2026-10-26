"""运行状态与日志总线：把自动化线程的状态安全地暴露给悬浮窗。

自动化逻辑跑在后台线程，tkinter 跑在主线程（tkinter 必须在主线程 mainloop），
两者只通过本模块通信：
- LogBus    ：把 print 出去的内容也收进一个环形缓冲，供悬浮窗显示
- FudaiStatus：房间／奖品／开奖时间／当前阶段／队列／统计的快照

两者都用锁保护、不做任何 IO，是纯内存对象，因此可以完整离线单测。
"""

import threading
import time
from collections import deque


class LogBus:
    """环形日志缓冲：Tee 每写一段就喂进来，悬浮窗取最后 N 行

    注意 print 的写入不保证按整行到达（可能一段里含多行、也可能半行），
    所以这里按 '\\n' 拆行，最后不完整的半行先攒着等下一次。
    """

    def __init__(self, max_lines=400):
        self.max_lines = max(10, int(max_lines))
        self._lines = deque(maxlen=self.max_lines)
        self._partial = ''
        self._lock = threading.Lock()

    def write(self, text):
        """接收任意大小的文本块"""
        if not text:
            return
        with self._lock:
            self._partial += text
            *complete, self._partial = self._partial.split('\n')
            for line in complete:
                self._lines.append(line.rstrip('\r'))

    def tail(self, count=12):
        """取最后 count 行（不足则全部）"""
        with self._lock:
            lines = list(self._lines)
        if count <= 0:
            return []
        return lines[-count:]

    def clear(self):
        with self._lock:
            self._lines.clear()
            self._partial = ''


class FudaiStatus:
    """自动化线程写入、悬浮窗线程读取的运行状态"""

    def __init__(self, now_fn=time.time):
        self._now = now_fn
        self._lock = threading.Lock()
        self._data = {
            'device_id': '',
            'room': '',
            'prize': '',
            'phase': '启动中',
            'draw_ts': 0.0,     # 正在等开奖的那个福袋的开奖时间戳；0 = 当前没在等
            'pending': [],      # 多房间队列里的任务 [{'room','prize','draw_ts'}, ...]
            # win/lose = 抽奖结果统计（未中的也要记）；done/missed/dropped = 多房间队列任务统计
            'stats': {'win': 0, 'lose': 0, 'done': 0, 'missed': 0, 'dropped': 0},
            'updated_ts': 0.0,
        }

    # ---- 写入 ----
    def _update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)
            self._data['updated_ts'] = self._now()

    def set_device(self, device_id):
        self._update(device_id=device_id or '')

    def set_phase(self, phase):
        self._update(phase=phase or '')

    def set_room(self, room, prize=None):
        if prize is None:
            self._update(room=room or '')
        else:
            self._update(room=room or '', prize=prize or '')

    def set_draw(self, draw_ts, prize=None):
        """登记"正在等这个福袋开奖" """
        with self._lock:
            self._data['draw_ts'] = float(draw_ts or 0)
            if prize:
                self._data['prize'] = prize
            self._data['updated_ts'] = self._now()

    def clear_draw(self):
        self._update(draw_ts=0.0)

    def set_pending(self, tasks):
        """更新多房间队列快照；tasks 为 [{'room','prize','draw_ts'}, ...]"""
        self._update(pending=[dict(item) for item in (tasks or [])])

    def set_stats(self, **stats):
        with self._lock:
            self._data['stats'].update(stats)
            self._data['updated_ts'] = self._now()

    def add_stat(self, key, delta=1):
        """累加一个统计值，返回更新后的完整统计（方便随日志一起打印）"""
        with self._lock:
            self._data['stats'][key] = self._data['stats'].get(key, 0) + delta
            self._data['updated_ts'] = self._now()
            return dict(self._data['stats'])

    # ---- 读取 ----
    def snapshot(self):
        """给悬浮窗用的只读快照，remaining 在此刻按 now 算好"""
        now = self._now()
        with self._lock:
            data = dict(self._data)
            pending = [dict(item) for item in self._data['pending']]
            data['stats'] = dict(self._data['stats'])
        for item in pending:
            draw_ts = float(item.get('draw_ts') or 0)
            item['remaining'] = max(0, int(round(draw_ts - now))) if draw_ts else None
        data['pending'] = pending
        data['draw_ts'] = float(data['draw_ts'] or 0)
        data['remaining'] = (max(0, int(round(data['draw_ts'] - now)))
                             if data['draw_ts'] else None)
        return data


def format_countdown(seconds):
    """把剩余秒数格式化成 mm:ss；超过一小时显示 h:mm:ss；None 显示 --:--"""
    if seconds is None:
        return '--:--'
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return '{}:{:02d}:{:02d}'.format(hours, minutes, secs)
    return '{:02d}:{:02d}'.format(minutes, secs)


def format_snapshot_lines(snapshot, include_countdown=True):
    """把状态快照格式化成悬浮窗上的文本行（纯函数，便于单测）

    include_countdown=False 时省略"开奖倒计时"那一行：
    悬浮窗把倒计时单独用大字号显示，不需要重复。
    """
    lines = []
    remaining = snapshot.get('remaining')
    if include_countdown:
        if remaining is None:
            lines.append('开奖倒计时：--:--（当前没有在等的福袋）')
        else:
            lines.append('开奖倒计时：{}'.format(format_countdown(remaining)))
    if remaining is not None and snapshot.get('prize'):
        lines.append('奖品内容：{}'.format(snapshot['prize']))
    if snapshot.get('room'):
        lines.append('当前房间：{}'.format(snapshot['room']))
    lines.append('当前状态：{}'.format(snapshot.get('phase') or '-'))
    pending = snapshot.get('pending') or []
    if pending:
        lines.append('排队中 {} 个：'.format(len(pending)))
        for item in pending[:5]:
            lines.append('   {}  {}'.format(
                format_countdown(item.get('remaining')), item.get('room') or '-'))
    stats = snapshot.get('stats') or {}
    win, lose = stats.get('win', 0), stats.get('lose', 0)
    lines.append('抽奖统计：中奖{}  未中{}  合计{}'.format(win, lose, win + lose))
    # 队列统计只在多房间功能真的产生过任务时才显示，免得一直挂个 0 占位置
    if any(stats.get(key, 0) for key in ('done', 'missed', 'dropped')):
        lines.append('队列统计：完成{}  错过{}  放弃{}'.format(
            stats.get('done', 0), stats.get('missed', 0), stats.get('dropped', 0)))
    return lines
