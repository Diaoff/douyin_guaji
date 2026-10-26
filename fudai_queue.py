"""多房间福袋排队调度：把"参与福袋"和"守着开奖"拆成可排队的任务。

为什么需要这个模块
------------------
一个手机一块屏，任何时刻只能待在一个直播间，所以"同时抢多个福袋"本质是
「参与 → 登记开奖时间 → 去别的房间继续找 → 快到点时回对应房间」的分时复用。

真正棘手的是**开奖时间相近**：回一次房间要花 arrive_margin 秒（进关注列表、
逐屏找房间、点进去、等页面稳定），处理一次开奖结果又要花 result_margin 秒
（等弹窗、领奖、下单、退回来）。所以每个任务实际占用一个**时间窗**：

    [开奖时间 - arrive_margin, 开奖时间 + result_margin]

两个任务的窗口只要重叠，就**不可能都守住**。本模块负责：
1. 把每个福袋任务变成带时间窗的预约
2. 检测窗口冲突，并按策略裁决谁"有保障"、谁降级为"伺机"
3. 到点了给出"该去哪个房间"

设计取舍
--------
冲突时**不直接丢弃**落败的那个，而是降级为"伺机"（best_effort）：
它留在队列里，一旦有保障的任务失败/提前结束（房间关闭、回房找不到、
开奖被取消），伺机任务还能顶上。同时伺机任务只在"不会耽误下一个有保障任务
出发"的前提下才会被服务。

本模块是纯逻辑，不依赖 adb / OCR / 当前时间（时间由调用方传入），
因此可以完整离线测试。
"""


class FudaiTask:
    """一个已参与的福袋，登记为一个待守护的开奖任务"""

    # 状态流转：pending -> serving -> done
    #                 \\-> missed（窗口过了没守住）
    #                 \\-> dropped（主动放弃，如回房失败）
    def __init__(self, room_key, prize, draw_ts, created_ts, value=1, need_watch=False):
        """
        Args:
            room_key: 房间标识（由房间标题归一化而来）
            prize: 福袋内容文字（OCR 结果）
            draw_ts: 预计开奖时间戳
            created_ts: 参与时间戳
            value: 价值分，冲突时用于 valuable_first 策略（越大越优先）
            need_watch: 是否"还需看播"（真机验证项：若必须留在房间看播，则不能离开）
        """
        self.room_key = room_key
        self.prize = prize
        self.draw_ts = float(draw_ts)
        self.created_ts = float(created_ts)
        self.value = int(value)
        self.need_watch = bool(need_watch)
        self.guaranteed = True     # 有保障：到点一定回房守它
        self.state = 'pending'
        self.reason = ''           # 降级/错过的原因，用于日志排查
        self.window_start = 0.0
        self.window_end = 0.0

    def describe(self):
        return "房间[{}] 奖品[{}] 开奖[{}] {}".format(
            self.room_key, self.prize, int(self.draw_ts),
            '有保障' if self.guaranteed else '伺机')

    def __repr__(self):
        return "<FudaiTask {} {} {}>".format(self.room_key, self.state,
                                             'G' if self.guaranteed else 'B')


class FudaiTaskQueue:
    """按开奖时间排队的福袋任务队列，负责时间窗预约与冲突裁决"""

    POLICIES = ('earlier_first', 'valuable_first', 'skip_new')

    def __init__(self, arrive_margin=60, result_margin=40, max_pending=5,
                 conflict_policy='earlier_first', travel_budget=45):
        """
        Args:
            arrive_margin: 开奖前多久必须开始往回赶（回房 + 等页面稳定的预算）
            result_margin: 开奖后还要占用多久（等弹窗 + 领奖 + 下单 + 退回来）
            max_pending: 队列里最多同时挂几个待守护任务
            conflict_policy: 窗口冲突时的裁决策略，见 POLICIES
            travel_budget: 从一个房间赶到另一个房间的耗时预算（伺机任务用它做安全余量）
        """
        self.arrive_margin = max(0, int(arrive_margin))
        self.result_margin = max(0, int(result_margin))
        self.max_pending = max(1, int(max_pending))
        if conflict_policy not in self.POLICIES:
            conflict_policy = 'earlier_first'
        self.conflict_policy = conflict_policy
        self.travel_budget = max(0, int(travel_budget))
        self.tasks = []

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _window(self, draw_ts):
        return draw_ts - self.arrive_margin, draw_ts + self.result_margin

    @staticmethod
    def _overlaps(a, b):
        return a.window_start <= b.window_end and b.window_start <= a.window_end

    def _pending(self):
        return [t for t in self.tasks if t.state == 'pending']

    def _wins(self, task, rival):
        """task 是否比 rival 更该被保障；平手时开奖早的优先"""
        if self.conflict_policy == 'valuable_first' and task.value != rival.value:
            return task.value > rival.value
        return task.draw_ts < rival.draw_ts

    def _next_guaranteed_start(self, now, exclude=None):
        starts = [t.window_start for t in self._pending()
                  if t.guaranteed and t is not exclude and t.window_start > now]
        return min(starts) if starts else None

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def add(self, task):
        """把任务加入队列，并按策略处理时间窗冲突

        Returns:
            (accepted: bool, reason: str)
        """
        pending = self._pending()
        if len(pending) >= self.max_pending:
            task.state = 'dropped'
            task.reason = '队列已满({})'.format(self.max_pending)
            return False, task.reason

        task.window_start, task.window_end = self._window(task.draw_ts)

        # 只管"有保障"的任务：伺机任务本来就让路，不与它判冲突
        conflicts = [t for t in pending if t.guaranteed and self._overlaps(t, task)]
        if not conflicts:
            self.tasks.append(task)
            return True, '加入队列（有保障）'

        if self.conflict_policy == 'skip_new':
            task.state = 'dropped'
            task.reason = '与{}开奖时间相近，按 skip_new 策略放弃参与'.format(
                conflicts[0].room_key)
            return False, task.reason

        if any(not self._wins(task, rival) for rival in conflicts):
            # 有对手比它更该被保障 —— 它降级为伺机，但仍留在队列里
            task.guaranteed = False
            task.reason = '与{}开奖时间相近，降级为伺机'.format(
                '、'.join(t.room_key for t in conflicts))
            self.tasks.append(task)
            return True, task.reason

        # 新任务更该被保障 —— 原来的冲突任务让路
        for rival in conflicts:
            rival.guaranteed = False
            rival.reason = '被开奖更早/更有价值的{}取代，降级为伺机'.format(task.room_key)
        self.tasks.append(task)
        return True, '新任务优先，取代{}'.format('、'.join(t.room_key for t in conflicts))

    def due(self, now):
        """返回此刻该去服务的任务；没有就返回 None

        规则：
        1. 先清理已经错过窗口的任务
        2. 有保障的任务里，取开奖最早的那个
        3. 只剩伺机任务时，只有在"不会耽误下一个有保障任务出发"的前提下才去
        """
        self.purge_expired(now)
        ready = [t for t in self._pending() if t.window_start <= now]
        if not ready:
            return None
        guaranteed = [t for t in ready if t.guaranteed]
        if guaranteed:
            return min(guaranteed, key=lambda t: t.draw_ts)
        next_start = self._next_guaranteed_start(now)
        if next_start is not None and now + self.travel_budget >= next_start:
            return None
        return min(ready, key=lambda t: t.draw_ts)

    def mark_serving(self, task):
        task.state = 'serving'

    def finish(self, task, state='done', reason=''):
        """结束一个任务；state 取 done / missed / dropped"""
        task.state = state
        if reason:
            task.reason = reason

    def purge_expired(self, now):
        """把窗口已过、还没被服务的任务标记为 missed

        Returns:
            list: 本次被标记为错过的任务（调用方可用于"去消息中心补领"）
        """
        expired = []
        for task in self._pending():
            if now > task.window_end:
                task.state = 'missed'
                task.reason = task.reason or '开奖窗口已过，没能在开奖前回到该直播间'
                expired.append(task)
        return expired

    def remove_room(self, room_key):
        """房间没了/标题变了时，把相关任务清掉"""
        for task in self._pending():
            if task.room_key == room_key:
                self.finish(task, 'dropped', '房间已不可用')

    # ------------------------------------------------------------------
    # 查询与统计
    # ------------------------------------------------------------------
    def counts(self):
        result = {'pending': 0, 'serving': 0, 'done': 0, 'missed': 0, 'dropped': 0}
        for task in self.tasks:
            if task.state in result:
                result[task.state] += 1
        return result

    def missed_tasks(self):
        """错过弹窗的任务 —— 若真机验证过"中奖可在消息中心补领"，这批可以事后补"""
        return [t for t in self.tasks if t.state == 'missed']

    def summary(self):
        pending = self._pending()
        guaranteed = [t for t in pending if t.guaranteed]
        return "队列：待守护{}（有保障{}／伺机{}） 完成{} 错过{} 放弃{}".format(
            len(pending), len(guaranteed), len(pending) - len(guaranteed),
            self.counts()['done'], self.counts()['missed'], self.counts()['dropped'])
