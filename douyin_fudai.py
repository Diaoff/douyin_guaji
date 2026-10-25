import atexit
import os
import random
import sys
import time
import numpy as np
from PIL import Image
from datetime import datetime
from Underlying_Operations import underlying_operations

VERSION = 'V4.4'  # 版本号唯一来源；README 里的"当前版本"需与本值保持一致


class Tee(object):
    """重写print的函数"""

    def __init__(self, *files):
        self.files = files

    def write(self, obj):
        for f in self.files:
            if getattr(f, 'closed', False):  # 文件已关闭时跳过，避免退出阶段写入报错
                continue
            f.write(obj)
            f.flush()

    def flush(self):
        for f in self.files:
            if getattr(f, 'closed', False):
                continue
            f.flush()


class fudai_analyse:
    """福袋抽奖相关操作类"""

    # ---- 时间窗（24小时制，闭区间）----
    # 旧代码把边界散落写成 1<=h<=7 / h>6 / 3<=h<=6 / h>7 几种写法，语义难以对齐；
    # 这里统一为具名常量，取值与旧版完全一致（行为不变，只消除魔法数字）。
    NIGHT_STOP_START = 1            # 夜间停机的起始小时（含）
    NIGHT_STOP_END = 7              # 夜间停机的结束小时（含）
    WAKE_HOUR = 8                   # 夜间等待结束、恢复挂机的小时（= NIGHT_STOP_END + 1）
    NO_REQUIRE_CONTENT_START = 1    # 该区间内不校验福袋内容是否是想要的
    NO_REQUIRE_CONTENT_END = 7
    NO_SWITCH_START = 0             # 该区间内不切换直播间
    NO_SWITCH_END = 6
    RESET_NO_FUDAI_START = 3        # 该区间内重置"长时间无福袋"计时
    RESET_NO_FUDAI_END = 6

    # ---- 其它阈值 ----
    BATTERY_LOW_THRESHOLD = 30      # 低于该电量进入待机等待
    # find_y_offset 中同一张裁剪图的 OCR 重复次数（config.json 的 ocr_retry_times 可覆盖）。
    # 旧实现固定为 2，但对完全相同的图片重复识别是纯浪费：实测 PaddleOCR 对同一张图结果确定。
    DEFAULT_OCR_RETRY_TIMES = 1
    MAX_OCR_RETRY_TIMES = 4
    # 连续多少轮"本轮无福袋"后主动换直播间（config.json 的 no_fudai_switch_rounds 可覆盖）。
    # 旧实现里 wait_times 只被置 0、从不自增，导致这条逻辑一直没生效；0 表示关闭该行为。
    DEFAULT_NO_FUDAI_SWITCH_ROUNDS = 4
    # 开奖倒计时窗口下界：剩余少于该秒数来不及点完，不参与
    DEFAULT_MIN_COUNTDOWN_SECONDS = 15
    # 运行时屏幕亮度（百分比）：挂机时调低省电，0 表示不做调整
    DEFAULT_SCREEN_BRIGHTNESS = 20

    def __init__(self):
        self.device_id = 'xxxxxxxx'
        self.y_pianyi = 0  # 应用于不同型号手机，图标相对屏幕的高度位置有偏差
        self.resolution_ratio_x = 1080
        self.resolution_ratio_y = 2400
        # 控制台编码保持不变，只把不可编码字符替换掉：让 README V4.4 里
        # "修复报错：UnicodeEncodeError: 'gbk' codec can't encode character" 真正成立
        try:
            sys.stdout.reconfigure(errors='replace')
        except (AttributeError, OSError, ValueError):
            pass
        timepic = datetime.now().strftime('%Y-%m-%d-%H-%M')
        self.log_file = open(timepic + '.log', 'w', encoding='utf-8')
        self.original_stdout = sys.stdout
        sys.stdout = Tee(sys.stdout, self.log_file)
        atexit.register(self.close_log)  # 进程退出时关闭日志文件句柄
        print("抖音福袋挂机 {} 启动，日志文件：{}".format(VERSION, self.log_file.name))
        self.last_find_fudai_time = 0.0
        self.last_refresh_zhibo_list_time = 0.0
        self.operation = underlying_operations()
        # self.config_path = "config.json"
        self.contains_not_want = self.operation.load_config("config.json", "contains_not_want")
        self.contains_want = self.operation.load_config("config.json", "contains_want")
        self.video_comments = self.operation.load_config("config.json", "video_comment")
        self.device_offsets = self.operation.load_config("config.json", "device_offsets")
        # 需要处理"停止充电"弹窗的设备（如 vivo），原先硬编码在代码里
        self.charging_notice_devices = self.operation.load_config("config.json", "charging_notice_devices")
        self.ocr_retry_times = self._load_ocr_retry_times()
        # 连续多少轮无福袋就主动换直播间；0 = 关闭该行为（一直等到直播间关闭）
        self.no_fudai_switch_rounds = self._load_int_config(
            "no_fudai_switch_rounds", self.DEFAULT_NO_FUDAI_SWITCH_ROUNDS, 0, 100)
        # 开奖倒计时窗口：太短来不及参与；太长（上限）先不参与
        self.min_countdown_seconds = self._load_int_config(
            "min_countdown_seconds", self.DEFAULT_MIN_COUNTDOWN_SECONDS, 0, 600)
        # 上限设为 0 表示沿用 fudai_choujiang(wait_minutes=...) 传入的值
        self.max_countdown_minutes = self._load_int_config("max_countdown_minutes", 0, 0, 60)
        # 挂机时把屏幕亮度调低省电；0 = 不调整
        self.screen_brightness = self._load_int_config(
            "screen_brightness", self.DEFAULT_SCREEN_BRIGHTNESS, 0, 100)
        self.current_page_type = 1
        self.page_name = {1: "直播间页", 2: "关注的正在直播列表页", 3: "个人中心关注页", 4: "订单支付完成页",
                          5: "首页-个人中心", 6: "首页-内容推荐", 7: "下单页"}

    @staticmethod
    def in_hour_range(hour, start, end):
        """判断小时是否落在闭区间[start, end]内；start>end 表示跨零点区间"""
        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end

    def _load_int_config(self, key, default, minimum, maximum):
        """从 config.json 读取一个整数配置项，非法值回退默认值、越界值收敛到范围内"""
        value = self.operation.load_config("config.json", key, default=default)
        try:
            number = int(value)
        except (TypeError, ValueError):
            print("config.json 中的 {} 不是整数({!r})，按默认值{}处理".format(key, value, default))
            return default
        if number < minimum:
            print("{}={} 小于下限，按{}处理".format(key, number, minimum))
            return minimum
        if number > maximum:
            print("{}={} 超过上限，按{}处理".format(key, number, maximum))
            return maximum
        return number

    def _load_ocr_retry_times(self):
        """读取 config.json 的 ocr_retry_times（同一张裁剪图的 OCR 重复次数）"""
        return self._load_int_config("ocr_retry_times", self.DEFAULT_OCR_RETRY_TIMES,
                                     1, self.MAX_OCR_RETRY_TIMES)

    def close_log(self):
        """关闭日志文件句柄，避免长时间运行导致句柄泄漏（由atexit兜底调用）"""
        log_file = getattr(self, 'log_file', None)
        if log_file is None or log_file.closed:
            return
        try:
            if sys.stdout is not getattr(self, 'original_stdout', None):
                sys.stdout = self.original_stdout  # 先还原标准输出，避免后续print写入已关闭的文件
        except Exception:
            pass
        try:
            log_file.close()
        except Exception:
            pass

    def check_robot_pic_distance(self):
        """计算滑动人机验证需要滑动的距离

        用 numpy 向量化重写，替代原来的逐像素双重循环（793x496 最坏情况约 33 万次
        getpixel 调用）；判定规则与原实现完全一致：
        1. 自上而下找到第一行里出现的第一个近白像素，作为滑动起点 (x, y)；
        2. 在同一行从 x 向右（含 x）找第一个深色缺口像素 x1，返回 x1 - x。
        任一步定位失败时返回 None，调用方会退回默认滑动距离。
        """
        self.cut_pic(143, 884, 936, 1380, '', 'robot_verification')
        path = os.path.dirname(__file__) + '/pic'
        pic = path + '/robot_verification.png'
        with Image.open(pic) as img:
            # 转 int16：三通道求和最大 765，用 uint8 会溢出导致误判
            arr = np.asarray(img.convert('RGB')).astype(np.int16)

        y_lo, y_hi = 20, arr.shape[0] - 30
        x_lo, x_hi = 5, arr.shape[1] - 40
        region = arr[y_lo:y_hi, x_lo:x_hi]

        # 1) 近白像素：nonzero 按行优先返回，取第一个即等价于原双重循环的搜索顺序
        white = (region[:, :, 0] > 240) & (region[:, :, 1] > 240) & (region[:, :, 2] > 240)
        rows, cols = np.nonzero(white)
        if rows.size == 0:
            print("未在人机验证图片中定位到滑动起点")
            return None
        row, col = int(rows[0]), int(cols[0])
        x, y = col + x_lo, row + y_lo
        print(x, y)

        # 2) 同一行向右找深色缺口（含起点本身，与原实现的 range(x, ...) 对齐）
        line = region[row]
        dark = ((line[:, 0] < 50) & (line[:, 1] < 55) & (line[:, 2] < 85)
                & (line[:, 0] + line[:, 1] + line[:, 2] < 150))
        dark[:col] = False
        dark_cols = np.nonzero(dark)[0]
        if dark_cols.size == 0:
            print("未在人机验证图片中定位到缺口位置")
            return None
        x1 = int(dark_cols[0]) + x_lo
        print(x1, y)
        print("需要滑动的距离为{}".format(x1 - x))
        return x1 - x

    def find_fudai_detail_top_y(self):
        """找到福袋详情顶部y轴的值"""
        y_range = (1700, 800)
        x = 30
        path = os.path.dirname(__file__) + '/pic'
        pic_path = path + '/screenshot.png'
        try:
            img = Image.open(pic_path)
            img_rgba = img.convert('RGBA')
            pix = img_rgba.load()
            # 参考分辨率坐标到实际像素坐标的换算
            x_pix = x * self.resolution_ratio_x // 1080
            start_y, end_y = y_range
            step = -5 if start_y > end_y else 5
            # 初始化第一个像素作为参考
            y_prev = start_y
            y_prev_pix = y_prev * self.resolution_ratio_y // 2400
            r_prev, g_prev, b_prev, _ = pix[x_pix, y_prev_pix]
            last_match_y = y_prev
            # 从起始y向终止y遍历（不包含终止边界超出）
            y = y_prev + step
            while (step < 0 and y >= end_y) or (step > 0 and y <= end_y):
                y_pix = y * self.resolution_ratio_y // 2400
                r, g, b, _ = pix[x_pix, y_pix]

                if abs(r - r_prev) <= 5 and abs(g - g_prev) <= 5 and abs(b - b_prev) <= 5:
                    last_match_y = y
                    # 更新上一像素作为新的比较基准
                    r_prev, g_prev, b_prev = r, g, b
                    y_prev = y
                else:
                    # 第一次出现不符合条件，返回最后一个符合条件的y
                    # print(f"找到福袋详情顶部纵坐标：{last_match_y}")
                    # 二次验证last_match_y是否有效
                    while last_match_y <= start_y:
                        y_verify_pix = last_match_y * self.resolution_ratio_y // 2400
                        x_verify_pix = 90 * self.resolution_ratio_x // 1080
                        try:
                            # 重新获取当前last_match_y位置的像素RGB值
                            r_current, g_current, b_current, _ = pix[x_pix, y_verify_pix]
                            # 重新获取(90, last_match_y)位置的像素RGB值
                            r_verify, g_verify, b_verify, _ = pix[x_verify_pix, y_verify_pix]
                            if (abs(r_verify - r_current) <= 2 and 
                                abs(g_verify - g_current) <= 2 and 
                                abs(b_verify - b_current) <= 2):
                                print(f"找到最终福袋详情顶部纵坐标：{last_match_y}")
                                return last_match_y
                        except IndexError:
                            # 如果坐标超出图像范围，跳出循环
                            break
                        last_match_y += 5
                        # print(f"验证失败，调整last_match_y到：{last_match_y}")
                    return last_match_y
                y += step
            print(f"扫描完成，最终福袋详情顶部纵坐标：{last_match_y}")
            return last_match_y
        except Exception as e:
            print(e)
            return None
        finally:
            try:
                img.close()
            except Exception:
                pass

    def find_y_offset(self):
        """查找设备对应的偏移值"""
        if self.device_id in self.device_offsets:
            print("已记录过该设备偏移值为{}".format(self.device_offsets[self.device_id]))
            return self.device_offsets[self.device_id]
        print("开始自动寻找合适的y轴偏移值...")
        test_offsets = range(0, 51, 2)  # 从0到50，步长为2
        self.operation.get_screenshot(self.device_id)
        matched_offsets = []  # 存储所有匹配的偏移值
        consecutive_failures = 0  # 连续失败次数
        found_first_match = False  # 是否找到第一个匹配值
        
        for offset in test_offsets:
            # offset=0 时 +0 与 -0 指向同一张裁剪图，去重可省掉一整轮重复识别
            for test_offset in dict.fromkeys([offset, -offset]):  # 测试正负两个值
                self.cut_pic(70, 2166+test_offset, 240, 2208+test_offset, '', 'say_something')
                matched = False
                # OCR 对同一张图是确定性的（已实测），因此默认只识别 1 轮；
                # 每轮会依次尝试两种二值化模式，两种模式的结果可能不同，故不去重。
                # 若个别机型出现漏识别，可在 config.json 里把 ocr_retry_times 调回 2。
                for loop in range(self.ocr_retry_times):
                    for i in [0, 2]:
                        say_something = self.operation.analyse_pic_word('say_something', i)
                        if "说点" in say_something:  # 说点什么
                            matched = True
                            if test_offset == 0:
                                print("偏移值为0")
                                return 0
                            matched_offsets.append(test_offset)
                            consecutive_failures = 0  # 重置连续失败计数
                            if not found_first_match:
                                found_first_match = True
                            break
                    if matched:
                        break
                    if loop + 1 < self.ocr_retry_times:  # 仅在还有下一轮时才等待
                        self.operation.delay(0.1)
                
                if not matched:
                    if found_first_match:  # 只有在找到第一个匹配值后才开始计数连续失败
                        consecutive_failures += 1
                        if consecutive_failures >= 6:  # 如果连续6次都不匹配,正负各3个值
                            if matched_offsets:  # 如果有匹配的值
                                avg_offset = round(sum(matched_offsets) / len(matched_offsets))
                                print(f"找到多个可能的偏移值，取平均值：{avg_offset}")
                                return int(avg_offset)
                            else:
                                print("界面未在直播间或未找到合适的偏移值,请联系叼哥处理")
                                return 0
                            
        # 如果遍历完所有值
        if matched_offsets:
            avg_offset = round(sum(matched_offsets) / len(matched_offsets))
            print(f"找到多个可能的偏移值，取平均值：{avg_offset}")
            return int(avg_offset)
        else:
            print("界面未在直播间或未找到合适的偏移值,请联系叼哥处理")
            return 0

    def check_have_fudai(self):
        """判定直播页面福袋的小图标是否存在
        Returns:
            int or False: 如果找到福袋，返回x坐标；否则返回False
        """
        path = os.path.dirname(__file__) + '/pic'
        pic_path = path + '/screenshot.png'
        max_loops = 5  # 最大循环次数
        check_interval = 1.5  # 检查间隔时间
        for loop in range(max_loops):
            self.operation.delay(check_interval)
            print(f"第{loop+1}次判定直播间是否存在福袋，共计会判定{max_loops}次")
            self.operation.get_screenshot(self.device_id)
            try:
                pic = Image.open(pic_path)
                pic_new = pic.convert('RGBA')
                pix = pic_new.load()
                # 颜色范围
                color_ranges = {
                    'r': (183, 193),
                    'g': (172, 193),
                    'b': (237, 248)
                }
                # 检查每个x坐标
                for base in [0, 126, 126 * 2, 126 * 3]:
                    for x in range(33+base, 52+base):
                        x_pos = x * self.resolution_ratio_x // 1080
                        if self.y_pianyi != 0:
                            # 检查三种偏移值的情况
                            for y_offset in [0, self.y_pianyi, -self.y_pianyi]:
                                y_pos = (403 + y_offset) * self.resolution_ratio_y // 2400
                                pixel_color = pix[x_pos, y_pos]
                                # 检查颜色是否在指定范围内
                                if (color_ranges['r'][0] <= pixel_color[0] <= color_ranges['r'][1] and
                                    color_ranges['g'][0] <= pixel_color[1] <= color_ranges['g'][1] and
                                    color_ranges['b'][0] <= pixel_color[2] <= color_ranges['b'][1]):
                                    self.last_find_fudai_time = time.time()
                                    if y_offset != 0 and y_offset != self.y_pianyi:
                                        self.y_pianyi = y_offset
                                        print(f"重新调整偏移值到{y_offset}")
                                    print(f"找到福袋X坐标：{x}")
                                    if pic:
                                        pic.close()
                                    if self.device_id not in self.device_offsets:
                                        self.device_offsets[self.device_id]=y_offset
                                        self.operation.update_config_with_offsets("config.json",{self.device_id:y_offset})
                                    return x
                        else:
                            y_pos = 403 * self.resolution_ratio_y // 2400
                            pixel_color = pix[x_pos, y_pos]
                            # 检查颜色是否在指定范围内
                            if (color_ranges['r'][0] <= pixel_color[0] <= color_ranges['r'][1] and
                                    color_ranges['g'][0] <= pixel_color[1] <= color_ranges['g'][1] and
                                    color_ranges['b'][0] <= pixel_color[2] <= color_ranges['b'][1]):
                                self.last_find_fudai_time = time.time()
                                print(f"找到福袋X坐标：{x}")
                                if pic:
                                    pic.close()
                                if self.device_id not in self.device_offsets:
                                    self.device_offsets[self.device_id] = 0
                                    self.operation.update_config_with_offsets("config.json", {self.device_id: 0})
                                return x
            except Exception as e:
                print(e)
                # 处理特殊情况
            if loop < 1 and self.check_zhibo_is_closed():
                print("第1次判定，检测到直播间已关闭")
                return False
            elif loop >= 4:
                self.deal_robot_analyse()
                return False
        print(f"本轮未判定到存在福袋")
        return False

    def browse_videos(self):
        """在视频推荐首页刷视频，随机点赞留评"""
        for i in range(10):
            if not self.check_in_homepage():
                self.operation.click_back(self.device_id)
                self.operation.delay(2)
                self.swipe(760, 1600, 760, 800, 200)
                print("上划切换视频")
                self.operation.delay(2)
                self.operation.get_screenshot(self.device_id)
                continue
            if random.random() < 1 / 2:
                self.click(990, 1180)  # 点击红心
                print("点击红心")
                self.operation.delay(1)
            if random.random() < 1 / 2:
                self.click(990, 1380)  # 点击评论
                print("点击留评")
                self.operation.delay(2)
                self.operation.get_screenshot(self.device_id)
                if self.check_have_comment():
                    self.click(320, 2180)
                    self.operation.delay(2)
                    comment = random.choice(self.video_comments)
                    print(f"输入评论内容：{comment}")
                    self.operation.input_text(self.device_id, comment)
                    self.operation.click_back(self.device_id)
                    self.operation.delay(2)
                    self.operation.get_screenshot(self.device_id)
                    if self.check_can_send_comment():
                        self.click(940, 2180)
                        print("点击发送评论")
                        self.operation.delay(2)
                        self.operation.click_back(self.device_id)
                        self.operation.delay(2)
            self.swipe(760, 1600, 760, 800, 200)
            print("上划切换视频")
            self.operation.random_delay(10, 30)

    def check_in_homepage(self):
        """判断是否在首页默认推荐页面"""
        self.cut_pic(50, 2150, 170, 2220, '', 'home_page')
        home_page = self.operation.analyse_pic_word('home_page')
        if "首页" in home_page:
            print("当前页面已在首页")
            return True
        return False

    def check_have_comment(self):
        """判断是否有留评页面"""
        self.cut_pic(66, 2150, 620, 2220, '', 'input_comment')
        input_comment = self.operation.analyse_pic_word('input_comment')
        if "恶言" in input_comment:
            print("当前可以添加评论")
            return True
        return False

    def check_can_send_comment(self):
        """判断是否有可以发送评论"""
        self.cut_pic(880, 2130, 1050, 2240, '', 'send_comment')
        send_comment = self.operation.analyse_pic_word('send_comment')
        if "发送" in send_comment:
            print("当前可以发送评论")
            return True
        return False

    def switch_to_focus_center(self):
        """初始化页面切换到直播间列表"""
        self.operation.get_screenshot(self.device_id)
        self.cut_pic(910, 2150, 1030, 2250, '', 'personal_center')
        personal_center = self.operation.analyse_pic_word('personal_center')
        if "我" in personal_center:
            self.click(970, 2200)
            print("切换页面到我的")
            self.operation.delay(2)
            self.operation.get_screenshot(self.device_id)
        self.cut_pic(380, 690, 490, 810, '', 'personal_center')
        personal_center = self.operation.analyse_pic_word('personal_center')
        if "关注" in personal_center:
            self.click(435, 750)
            print("打开关注列表")
            self.operation.delay(2)
            return True
        return False

    def deal_sign_in_gold_coins(self):
        """处理打开首页的签到领金币弹窗"""
        self.operation.get_screenshot(self.device_id)
        self.cut_pic(265, 1615, 780, 1720, '', 'sign_in')
        sign_in = self.operation.analyse_pic_word('sign_in')
        if "签到" in sign_in:
            self.click(970, 2200)
            print("点击签到领取金币")
            self.operation.delay(3)
            self.click(970, 2200)
            print("点击继续看视频")
            self.operation.delay(2)

    def cut_pic(self, x_top=1, y_top=1, x_bottom=2, y_bottom=2, target='', cut_pic_name=''):
        """裁剪图片方法"""
        self.operation.cut_pic(
            (x_top * self.resolution_ratio_x // 1080, y_top * self.resolution_ratio_y // 2400),
            (x_bottom * self.resolution_ratio_x // 1080, y_bottom * self.resolution_ratio_y // 2400),
            target,
            cut_pic_name)

    def check_have_robot_analyse(self):
        """检查是否存在人机校验"""
        self.cut_pic(130, 790, 680, 870, '', 'zhibo_yanzheng')
        result = self.operation.analyse_pic_word('zhibo_yanzheng', 1)
        if "验证" in result:
            print("存在滑动图片人机校验，需要等待完成验证.")
            return 1
        elif "形状相同" in result:
            print("存在点击图片人机校验，需要等待完成验证.")
            return 2
        self.cut_pic(340, 1415, 700, 1518, '', 'zhibo_yanzheng')
        result = self.operation.analyse_pic_word('zhibo_yanzheng')
        if "开始检测" in result:
            print("存在人脸识别人机校验，需要等待完成验证.")
            return 3
        return False

    def deal_swipe_robot_analyse(self, distance=400):
        """处理滑动图片的人机验证"""
        if distance:
            targetx = (222 + distance) * self.resolution_ratio_x // 1080
        else:
            targetx = 622 * self.resolution_ratio_x // 1080
        self.swipe(222, 1444, targetx, 1444, 300)
        print("滑轨滑动{}距离解锁人机验证".format(distance))
        self.operation.delay(1)

    def deal_robot_analyse(self):
        """处理人机校验，包含各种情况
        
        处理三种类型的人机验证：
        1. 滑动图片验证
        2. 点击图片验证
        3. 人脸识别验证
        
        Returns:
            bool: 是否成功处理了人机验证
        """
        max_swipe_times = 10
        wait_time = 1800  # 30分钟等待时间
        
        def handle_slide_verification():
            """处理滑动图片验证"""
            distance = self.check_robot_pic_distance()
            self.deal_swipe_robot_analyse(distance)
            self.operation.delay(10)
            self.operation.get_screenshot(self.device_id)
            
        def handle_click_verification():
            """处理点击图片验证"""
            print("无法处理图片验证的人机，点击关闭退出验证，等待30分钟")
            self.click(910, 800)  # 点击关闭
            self.operation.delay(wait_time)
            
        def handle_face_verification():
            """处理人脸识别验证"""
            print("无法处理人脸识别验证的人机，点击返回退出验证，等待30分钟")
            self.operation.click_back(self.device_id)
            self.operation.delay(2)
            self.operation.click_back(self.device_id)
            self.operation.delay(wait_time)
            
        # 验证处理映射
        verification_handlers = {
            1: handle_slide_verification,
            2: handle_click_verification,
            3: handle_face_verification
        }
        
        for swipe_times in range(max_swipe_times):
            robot_result = self.check_have_robot_analyse()
            
            # 如果没有验证，直接返回
            if not robot_result:
                return True
                
            # 获取对应的处理函数并执行
            handler = verification_handlers.get(robot_result)
            if handler:
                handler()
                if robot_result in (2, 3):  # 如果是无法处理的验证类型，直接退出
                    return False
            else:
                return False
                
        # 如果超过最大尝试次数，按点击验证处理
        print("无法处理图片验证的人机，点击关闭退出验证，等待30分钟")
        self.click(910, 800)
        self.operation.delay(wait_time)
        return False

    def swipe(self, left_up_x=0, left_up_y=0, right_down_x=1080, right_down_y=1500, steps=200):
        """调整比例后划动屏幕"""
        left_up_x = left_up_x * self.resolution_ratio_x // 1080
        right_down_x = right_down_x * self.resolution_ratio_x // 1080
        left_up_y = left_up_y * self.resolution_ratio_y // 2400
        right_down_y = right_down_y * self.resolution_ratio_y // 2400
        self.operation.swipe(self.device_id, left_up_x, left_up_y, right_down_x, right_down_y, steps)

    def click(self, x=500, y=500):
        """调整比例后点击坐标位置"""
        x = x * self.resolution_ratio_x // 1080
        y = y * self.resolution_ratio_y // 2400
        self.operation.click(self.device_id, x, y)

    def reflash_zhibo(self):
        """在关注列表，下拉刷新直播间"""
        current_time = time.time()
        wait_time = current_time - self.last_refresh_zhibo_list_time
        wait_time = round(wait_time, 1)
        if wait_time > 600:
            print("下划刷新直播间列表")
            self.swipe(760, 700, 760, 1500)
            self.last_refresh_zhibo_list_time = time.time()
            self.operation.delay(5)
            return True
        print("10分钟内已刷新过直播间列表，不再刷新")
        return False

    def check_in_follow_list(self):
        """判断是否界面在我的关注的列表页"""
        self.cut_pic(244, 125, 875, 220, '', 'zhibo_follow_list')
        zhibo_list_title = self.operation.analyse_pic_word('zhibo_follow_list')
        if "关注" in zhibo_list_title and "粉丝" in zhibo_list_title:
            print("当前界面在用户关注列表")
            return True
        return False

    def check_in_zhibo_list(self):
        """检查是否当前在关注的直播列表"""
        self.cut_pic(400, 125, 675, 230, '', 'zhibo_list_title')
        zhibo_list_title = self.operation.analyse_pic_word('zhibo_list_title')
        if "正在直播" in zhibo_list_title:
            print("当前界面已经在关注的直播间列表")
            return True
        self.cut_pic(80, 290, 270, 380, '', 'zhibo_list_title')
        zhibo_list_title = self.operation.analyse_pic_word('zhibo_list_title')
        if "直播中" in zhibo_list_title:
            print("当前界面已经在关注的直播间列表")
            return True
        return False

    def check_zhibo_is_closed(self):
        """检查当前直播间是否关闭"""
        self.cut_pic(350, 100, 740, 300, '', 'zhibo_status')
        zhibo_list_title = self.operation.analyse_pic_word('zhibo_status')
        if "已结束" in zhibo_list_title:
            print("当前直播间已关闭")
            return True
        print("当前直播间未判定到关闭")
        return False

    def check_zhibo_is_closed_guess_whatyoulike(self):
        """检查当前直播间是否关闭-判断猜你喜欢的位置"""
        self.cut_pic(440, 1570, 640, 1640, '', 'zhibo_status')
        zhibo_list_title = self.operation.analyse_pic_word('zhibo_status', 1)
        if "猜你喜欢" in zhibo_list_title:
            print("当前直播间已关闭")
            return True
        return False

    def back_to_zhibo_list(self):
        """功能初始化，回到直播间列表"""
        click_back_times = 0
        while click_back_times < 4:
            self.operation.get_screenshot(self.device_id)
            if self.check_in_zhibo_list():
                return False
            elif self.check_in_follow_list():
                self.click(390, 590)
                print("点击打开直播间的列表")
                self.operation.delay(2)
                return False
            self.operation.click_back(self.device_id)
            self.operation.delay(2)
            click_back_times += 1

    def into_zhibo_from_list(self):
        """从直播列表进入直播间"""
        while True:
            self.operation.get_screenshot(self.device_id)
            if self.check_in_zhibo_list():
                current_hour = self.operation.get_current_hour()
                if self.in_hour_range(current_hour, self.NIGHT_STOP_START, self.NIGHT_STOP_END):
                    print("等待10分钟继续检查")
                    self.operation.delay(600)  # 等待10分钟继续检查
                else:
                    if self.device_id in self.charging_notice_devices:
                        if self.check_stop_charging():
                            self.operation.click(self.device_id, 540, 2153)
                    if self.reflash_zhibo():  # 刷新直播间列表
                        if current_hour >= self.WAKE_HOUR:  # 如果当前时间已经早上8点多了，一定有直播间了
                            self.click(290, 490)  # 点击第一个直播间
                            print("点击打开第一个直播间")
                            break  # 跳出循环，直播间已找到
                        elif self.check_zhibo_list_have_zhibo():  # 如果存在直播间
                            self.click(290, 490)  # 点击第一个直播间
                            print("点击打开第一个直播间")
                            break
                        else:  # 如果直播列表是空的，则退出到关注列表
                            self.operation.click_back(self.device_id)
                            self.operation.delay(3)
                            print("点击退出到关注列表")
                    else:
                        self.click(810, 490)  # 点击第一个直播间
                        print("点击打开第2个直播间")
                        break
            elif self.check_zhibo_have_popup():
                self.click(540, 1620)
                print("点击关闭红包弹窗")
                self.operation.click_back(self.device_id)
                self.operation.delay(3)
                print("点击退出直播间")
            elif self.check_zhibo_is_closed():
                self.operation.click_back(self.device_id)
                self.operation.delay(3)
                print("点击退出直播间")
            elif self.check_in_follow_list():
                self.click(390, 590)
                print("点击打开直播间的列表")
                self.operation.delay(2)
            else:
                if self.device_id in self.charging_notice_devices:
                    if self.check_stop_charging():
                        self.operation.click(self.device_id, 540, 2153)
                print("当前页面不在直播间,等待10分钟后再检查")
                self.operation.delay(600)  # 等待10分钟继续检查

    def check_zhibo_list_have_zhibo(self):
        """检查直播列表是否存在直播的内容"""
        self.operation.get_screenshot(self.device_id)
        path = os.path.dirname(__file__) + '/pic'
        pic1_path = path + '/screenshot.png'
        pic = Image.open(pic1_path)
        try:
            pic_new = pic.convert('RGBA')
            pix = pic_new.load()
            if pix[290 * self.resolution_ratio_x // 1080, 490 * self.resolution_ratio_y // 2400][0] == 255 and \
                    pix[290 * self.resolution_ratio_x // 1080, 490 * self.resolution_ratio_y // 2400][1] == 255 and \
                    pix[290 * self.resolution_ratio_x // 1080, 490 * self.resolution_ratio_y // 2400][2] == 255:
                print('直播间列表为空')
                return False
            print('直播间列表存在直播的内容')
            return True
        finally:
            pic.close()

    def check_zhibo_have_popup(self):
        """判断直播间是否弹出了节假日红包弹窗"""
        self.cut_pic(425, 880, 660, 960, '', 'zhibo_hongbao')
        zhibo_list_title = self.operation.analyse_pic_word('zhibo_hongbao', 1)
        if "最高金额" in zhibo_list_title:
            print("直播间有红包弹窗")
            return True
        return False

    def check_no_fudai_time(self):
        """无福袋等待时间检查"""
        current_hour = self.operation.get_current_hour()
        if self.in_hour_range(current_hour, self.RESET_NO_FUDAI_START, self.RESET_NO_FUDAI_END):  # 凌晨3-6点
            self.last_find_fudai_time = 0.00
        elif self.last_find_fudai_time == 0.00 or self.last_find_fudai_time == 0:  # 如果过了不挂机时间，把当前时间赋值给上次找到福袋的时间
            self.last_find_fudai_time = time.time()
        if self.last_find_fudai_time > 0:
            current_time = time.time()
            wait_time = current_time - self.last_find_fudai_time
            wait_time = round(wait_time, 1)
            if wait_time > 18000:
                self.last_find_fudai_time = 0.00  # 重置找到福袋的时间
                wait_time = 0
            if wait_time > 1:
                print("距离上一次识别到福袋已经过去{}秒".format(wait_time))
            return wait_time
        return 0

    def get_fudai_contain_and_countdown(self, top_y=1000):
        """截取福袋的内容和倒计时，并识别内容"""
        if not isinstance(top_y, int) or isinstance(top_y, bool) or top_y <= 0:
            print(f"福袋详情顶部坐标异常({top_y})，跳过本轮内容识别")
            return '', ''
        self.cut_pic(390, top_y + 265,
                     1010, top_y + 475,
                     '', 'fudai_content')
        self.cut_pic(690, top_y + 0,
                     970, top_y + 240,
                     '', 'fudai_countdown')
        fudai_content_text = self.operation.analyse_pic_word('fudai_content', 1)
        time_text = self.operation.analyse_pic_word('fudai_countdown', 2)
        print(f"福袋内容：{fudai_content_text}")
        print(f"倒计时时间：{time_text}")
        return fudai_content_text, time_text

    def check_contain(self, contains='', current_hour=None):
        """检查福袋内容是否是想要，从配置文件config中读取"""
        if current_hour is None:
            current_hour = self.operation.get_current_hour()
        if self.in_hour_range(current_hour, self.NO_REQUIRE_CONTENT_START, self.NO_REQUIRE_CONTENT_END):
            print("半夜不对福袋内容做要求")
            return False
        for contain in self.contains_want:
            if contain in contains:
                print("福袋内容是想要的")
                return False
        for contain in self.contains_not_want:
            if contain in contains:
                print("福袋内容不是想要的")
                return True
        print("福袋内容未明确是否想要")
        return False

    def attend_choujiang(self):
        """点击参与抽奖
        Returns:
            bool: 是否成功参与抽奖
        """
        def close_fudai_detail():
            """关闭福袋详情"""
            self.click(500, 470)
            print("点击福袋外部，关闭福袋详情")
            self.operation.delay(1)
            
        def handle_special_buttons():
            """处理特殊按钮情况"""
            if "加入粉丝团" in attend_button_text and "钻石" in attend_button_text:
                close_fudai_detail()
                return False
            elif "活动已结束" in attend_button_text:
                close_fudai_detail()
                return False
            elif "开通店铺会员" in attend_button_text:
                self.operation.click_back(self.device_id)
                self.operation.delay(1)
                close_fudai_detail()
                return False
            elif "加入粉丝团（1钻石）" in attend_button_text:
                close_fudai_detail()
                return False
            return None
            
        def handle_participation_buttons():
            """处理参与按钮情况"""
            if "评论" in attend_button_text or "参与抽奖" in attend_button_text:
                self.click(500, 2060)
                print("点击参与抽奖")
                self.operation.delay(2)
                return True
            elif "开始观看" in attend_button_text:
                self.click(500, 2060)
                print("点击开始观看直播，参与抽奖")
                return True
            elif "粉丝团" in attend_button_text:
                self.click(500, 2060)
                print("点击加入粉丝团、点亮粉丝团")
                self.operation.delay(2)
                close_fudai_detail()
                return False
            return None
            
        def handle_status_buttons():
            """处理状态按钮情况"""
            if "参与成功" in attend_button_text:
                print("已经参与，等待开奖")
                close_fudai_detail()
                return True
            elif "还需看播" in attend_button_text:
                print("已经参与，等待看播时间凑齐开奖")
                close_fudai_detail()
                return True
            elif "无法参与" in attend_button_text:
                print("条件不满足，无法参与抽奖")
                close_fudai_detail()
                return False
            elif "时长不足" in attend_button_text:
                print("看播时长不够了，无法参与抽奖")
                close_fudai_detail()
                return False
            elif "不满足参与条件" in attend_button_text:
                print("不满足参与条件，无法参与抽奖")
                close_fudai_detail()
                return False
            return None

        click_times = 0
        while click_times < 2:
            self.cut_pic(306, 2030, 780, 2130, '', 'attend_button')
            attend_button_text = self.operation.analyse_pic_word('attend_button', 1)
            print(f"参与抽奖按钮文字内容：{attend_button_text}")
            
            # 处理特殊按钮情况
            result = handle_special_buttons()
            if result is not None:
                return result
                
            # 处理参与按钮情况
            result = handle_participation_buttons()
            if result is not None:
                # 第一次点击参与后，立即二次判定是否出现“开始观看”等需要第二次点击的状态
                self.operation.get_screenshot(self.device_id)
                self.cut_pic(306, 2030, 780, 2130, '', 'attend_button')
                attend_button_text = self.operation.analyse_pic_word('attend_button', 1)
                print(f"二次判定参与抽奖按钮文字内容：{attend_button_text}")
                result_second = handle_participation_buttons()
                if result_second is not None:
                    return result_second
                return result
                
            # 处理状态按钮情况
            result = handle_status_buttons()
            if result is not None:
                return result
                
            # 未匹配到任何情况
            print("参与抽奖按钮文字没匹配上")
            click_times += 1
            
        print("参与抽奖多次点击失败")
        return False

    def check_lucky_draw_result(self):
        """判定福袋抽奖的结果"""
        self.operation.get_screenshot(self.device_id)
        self.cut_pic(350, 655, 740, 755, '', 'lucky_draw_result')  # 没有抽中福袋位置
        lucky_draw_result = self.operation.analyse_pic_word('lucky_draw_result')
        if "没有抽中" in lucky_draw_result:
            self.cut_pic(340, 1200, 730, 1350, '', 'no_award_confirm')  # 我知道了按钮的位置
            no_award_confirm = self.operation.analyse_pic_word('no_award_confirm')
            if "我知道了" in no_award_confirm:
                return 1
            elif "领取并使用" in no_award_confirm:
                return 2
            return 3
        elif "抽中福袋" in lucky_draw_result:
            print("恭喜中奖了！")
            y_value = [1438, 1490]
            for y in y_value:
                self.cut_pic(280, y - 30, 660, y + 30, '', 'have_read_user_agreement')  # 已经阅读并同意用户协议
                have_read_user_agreement = self.operation.analyse_pic_word('have_read_user_agreement')
                if "已阅读" in have_read_user_agreement:
                    return y
        return False

    def check_have_reward_notice_confirm(self):
        """判断是否有领奖的二次确认提醒"""
        self.operation.get_screenshot(self.device_id)
        self.cut_pic(370, 1350, 680, 1440, '', 'reward_notice_confirm')  # 提醒领取奖品的弹窗
        reward_notice_confirm = self.operation.analyse_pic_word('reward_notice_confirm', 1)
        if "我知道了" in reward_notice_confirm:
            print("存在奖品领取提醒")
            return True
        return False

    def check_in_order_confirm_page(self):
        """判断是否在中奖下单后的购买成功的页面"""
        self.operation.get_screenshot(self.device_id)
        self.cut_pic(370, 120, 700, 220, '', 'order_confirm')  # 提醒领取奖品的弹窗
        order_confirm = self.operation.analyse_pic_word('order_confirm')
        if "购买成功" in order_confirm:
            print("福袋商品下单成功！")
            return True
        return False

    def get_reward(self, reward_y=0):
        """中奖后领奖然后返回"""
        self.operation.save_reward_pic(self.device_id)
        self.click(243, reward_y)  # 勾选协议
        self.operation.delay(1)
        self.click(540, reward_y - 140)  # 点击领取
        print("勾选协议，点击领取奖品")
        self.operation.delay(5)
        self.click(886, 2170)  # 点击下单
        print("点击下单")
        self.operation.delay(10)
        while self.check_in_order_confirm_page():
            # self.click(80, 180)  # 点击回退按钮
            self.operation.click_back(self.device_id)
            print("点击回退按钮")
            self.operation.delay(4)
        print("下完单返回到直播间")
        self.operation.delay(4)
        if self.check_lucky_draw_result():
            print("领奖弹窗未关闭，点击关闭弹窗")
            self.click(540, reward_y + 180)
            print("点击坐标位置:540 {}关闭领奖弹窗".format((reward_y + 180) * self.resolution_ratio_y // 2400))
            self.operation.delay(2)
        if self.check_have_reward_notice_confirm():
            print("提醒领奖弹窗未关闭，点击我知道了，关闭弹窗")
            self.click(540, 1400)  # 点击我知道了
            self.operation.delay(2)
        self.operation.delay(30)
        print("关闭中奖提醒后等待30S")

    def deal_battery_level(self):
        """针对电量不足的情况做处理"""
        while True:
            battery_level = self.operation.get_ballery_level(self.device_id)
            if battery_level is None:
                # 电量读取失败时跳过本轮判定；旧实现会因 None < 30 直接抛 TypeError
                print("电量读取失败，本轮跳过电量判定")
                return
            if battery_level >= self.BATTERY_LOW_THRESHOLD:
                return
            print("设备电量较低，退出到直播列表，等待电量恢复后继续挂机")
            self.back_to_zhibo_list()
            self.operation.delay(1800)  # 挂机30分钟

    def apply_screen_brightness(self):
        """挂机时调低屏幕亮度省电，退出时自动还原

        亮度是背光/面板级设置，不改变 screencap 截到的画面内容，
        因此降低亮度不会影响 OCR 识别和福袋图标的像素颜色判定。
        亮度值由 config.json 的 screen_brightness 控制（1~100，0=不调整）。
        """
        if self.screen_brightness <= 0:
            print("screen_brightness=0，保持系统亮度不做调整")
            return False
        if not self.operation.set_screen_brightness(self.device_id, self.screen_brightness):
            return False
        # 退出时还原；restore 是幂等的，重复注册无副作用
        atexit.register(self.operation.restore_screen_brightness)
        return True

    def check_stop_charging(self):
        """针对vivo设备，判断长时间连接弹出停止充电的弹窗"""
        self.cut_pic(225, 1951, 865, 2051, '', 'charging_stop_notice')
        charging_stop_notice = self.operation.analyse_pic_word('charging_stop_notice')
        if "充电" in charging_stop_notice:
            print("设备弹出停止充电提醒！")
            return True
        return False

    def restart_and_reset(self, max_retries=5):
        """重启抖音并页面复位到直播间（循环重试，避免递归耗尽调用栈）

        Returns:
            bool: 是否成功把页面复位到直播间
        """
        for attempt in range(1, max_retries + 1):
            self.operation.start_douyin_app(self.device_id)
            self.operation.delay(6)  # 开屏5秒广告
            self.deal_sign_in_gold_coins()
            self.browse_videos()
            if self.switch_to_focus_center():
                self.back_to_zhibo_list()
                self.into_zhibo_from_list()
                return True
            print(f"第{attempt}次启动抖音后未回到关注页，关闭应用后重试")
            self.operation.stop_douyin_app(self.device_id)
            self.operation.delay(10)
        print(f"连续{max_retries}次启动抖音后仍未回到关注页，交回主流程处理")
        return False

    def fudai_choujiang(self, device_id="", needswitch=False, wait_minutes=15):
        """福袋抽奖主逻辑，默认不切换直播间"""
        def initialize():
            """初始化参数"""
            self.device_id = device_id
            self.resolution_ratio_x, self.resolution_ratio_y = self.operation.get_device_resolution(self.device_id)
            self.apply_screen_brightness()  # 先调低亮度省电，再做后续识别
            self.y_pianyi = self.find_y_offset()
            return {
                'wait_times': 0,  # 当前直播间的等待次数
                'swipe_times': 0,  # 向上滑动的次数
                'fudai_not_open_times': 0  # 无法打开福袋的次数
            }

        def handle_night_time(current_hour):
            """处理凌晨时间段"""
            if self.in_hour_range(current_hour, self.NIGHT_STOP_START, self.NIGHT_STOP_END):
                # self.back_to_zhibo_list()
                self.operation.stop_douyin_app(self.device_id)
                wait_time = self.WAKE_HOUR - current_hour
                print(f"已经凌晨{current_hour}点了，关闭抖音，等待{wait_time}个小时")
                self.operation.delay(3600*wait_time)
                self.restart_and_reset()
                return True
            return False

        def handle_no_fudai_time():
            """处理长时间无福袋的情况"""
            if self.check_no_fudai_time() > 1800:  # 如果30分钟都没有福袋
                self.operation.save_reward_pic(self.device_id)
                self.back_to_zhibo_list()
                self.into_zhibo_from_list()
                return True
            return False

        def handle_fudai_click(x, stats):
            """处理福袋点击"""
            if x:
                stats['wait_times'] = 0
                self.click(x + 25, 430 + self.y_pianyi)
                print("点击打开福袋详情")
                self.operation.delay(3)
                return True
            return False

        def handle_switch_live_room(stats):
            """处理切换直播间的情况"""
            current_hour = self.operation.get_current_hour()
            if stats['swipe_times'] < 10 and not self.in_hour_range(
                    current_hour, self.NO_SWITCH_START, self.NO_SWITCH_END):
                self.swipe(760, 1600, 760, 800, 200)
                print("上划切换直播间")
                stats['swipe_times'] += 1
            else:
                print("已切换10个直播间，退出返回直播列表")
                self.operation.click_back(self.device_id)
                self.operation.delay(3)
                if self.check_in_follow_list():
                    self.click(390, 560)
                    print("点击打开直播间列表")
                self.into_zhibo_from_list()
                stats['swipe_times'] = 0
            self.operation.delay(5)
            return True

        def handle_closed_live_room(stats):
            """处理直播间关闭的情况"""
            if self.check_zhibo_is_closed():
                self.back_to_zhibo_list()
                self.into_zhibo_from_list()
                stats['swipe_times'] = 0
                self.operation.delay(5)
                return True
            return False

        def handle_live_room_list(stats):
            """处理直播间列表的情况"""
            if self.check_in_zhibo_list():
                self.into_zhibo_from_list()
                stats['swipe_times'] = 0
                return True
            return False

        def handle_wait_time(stats):
            """连续若干轮判定不到福袋就换直播间

            旧实现判断 stats['wait_times'] >= 4，但 wait_times 全代码只被置 0、从不自增，
            因此这条逻辑从未生效（房间没福袋时会一直等到 30 分钟兜底或房间关闭）。
            no_fudai_switch_rounds=0 可关闭该行为，恢复"一直等到房间关闭"。
            """
            if self.no_fudai_switch_rounds <= 0:
                return False
            if stats['wait_times'] >= self.no_fudai_switch_rounds:
                if stats['swipe_times'] < 10:
                    self.swipe(760, 1600, 760, 800, 200)
                    print("连续{}轮无福袋，上划切换直播间".format(stats['wait_times']))
                    stats['swipe_times'] += 1
                else:
                    print("连续{}轮无福袋且已切换10个直播间，退出返回直播列表".format(stats['wait_times']))
                    self.back_to_zhibo_list()
                    self.into_zhibo_from_list()
                    stats['swipe_times'] = 0
                stats['wait_times'] = 0
                self.operation.delay(5)
                return True
            return False

        def get_fudai_info():
            """获取福袋信息：顶部坐标识别失败时重试，仍失败则返回空文本交给上层降级处理"""
            for attempt in range(2):
                self.operation.get_screenshot(self.device_id)
                y_top = self.find_fudai_detail_top_y()
                if y_top:
                    return self.get_fudai_contain_and_countdown(y_top)
                print(f"第{attempt + 1}次未识别到福袋详情顶部坐标，重试")
                self.operation.delay(2)
            print("未识别到福袋详情顶部坐标，本轮按打开福袋异常处理")
            return '', ''

        def handle_fudai_content(x, stats, fudai_content_text, current_hour):
            """处理福袋内容"""
            if self.check_contain(fudai_content_text, current_hour):
                self.click(x + 45, 430)
                print("点击小福袋位置，关闭福袋详情")
                self.operation.delay(1)
                print("直播间福袋内容不理想")
                handle_switch_live_room(stats)
                # self.swipe(760, 1600, 760, 800, 200)
                # stats['swipe_times'] += 1
                # self.operation.delay(6)
                return True
            return False

        def handle_countdown(x, stats, time_text):
            """处理倒计时"""
            result = self.operation.check_countdown(time_text)
            if result:
                stats['fudai_not_open_times'] = 0
                lastsecond, future_timestamp = result
                return lastsecond, future_timestamp
            # 如果第一次失败，重新获取信息再试一次
            self.operation.delay(2)
            self.operation.get_screenshot(self.device_id)
            _, time_text = get_fudai_info()
            result = self.operation.check_countdown(time_text)
            if result:
                stats['fudai_not_open_times'] = 0
                lastsecond, future_timestamp = result
                return lastsecond, future_timestamp
            # 如果两次尝试都失败
            stats['fudai_not_open_times'] += 1
            self.click(x + 45, 470)
            print(f"第{stats['fudai_not_open_times']}次打开福袋异常，点击小福袋下方位置，关闭福袋详情")
            if stats['fudai_not_open_times'] > 10:
                print("超过10次点击福袋无法打开详情，等待30分钟")
                self.operation.delay(1800)
            self.operation.delay(2)
            return None

        def handle_lucky_draw(x):
            """处理抽奖结果"""
            # 检查抽奖结果
            result = self.check_lucky_draw_result()
            if result:
                if result == 1:
                    self.click(540, 1270)
                    print("没有抽中，点击：我知道了，关闭弹窗")
                    self.operation.delay(5)
                    # 二次检验：5秒后再次判断弹窗是否仍存在，若存在则再次点击关闭
                    second_check = self.check_lucky_draw_result()
                    if second_check:
                        self.click(540, 1270)
                        print("二次点击：我知道了，关闭弹窗")
                        self.operation.delay(2)
                    return True
                elif result == 2:
                    self.click(540, 1270)
                    print("没有抽中但获得优惠券，点击：领取并使用，关闭弹窗")
                    self.operation.delay(2)
                    self.click(x, 400)
                    print("点击小福袋偏上的位置，关闭店铺商品列表")
                    self.operation.delay(1)
                    return True
                elif result == 3:  # 其他情况
                    print("没有抽中但交互按钮未知，等待5秒后继续")
                    self.operation.delay(5)
                    return False
                else:  # 中奖了
                    print(f"抽中奖品，开始领取，奖品确认按钮y坐标为：{result}")
                    self.get_reward(result)
                    return True
            return False

        def handle_countdown_check(x, stats, lastsecond, needswitch, max_countdown_minutes):
            """开奖倒计时窗口判定：太短来不及、太长先不参与

            旧实现把两个阈值都以 needswitch 为前提，而入口写死 needswitch=False，
            因此默认的"不切换直播间"模式下这两条规则完全不生效（剩 3 秒也会照样参与并干等）。
            现在两种模式都会判定，区别只在"不参与之后怎么办"：
            - needswitch=True：关掉详情后上划换直播间（原行为）
            - needswitch=False：关掉详情后原地等一会儿再看，不换房间
            注意：max_countdown_minutes 只有设成小于 15 才真正可能触发，
            因为 check_countdown() 会把超过 900 秒的读数当识别异常压到 890 秒。
            """
            too_late = 0 <= lastsecond < self.min_countdown_seconds
            too_early = max_countdown_minutes > 0 and lastsecond >= 60 * max_countdown_minutes
            if not (too_late or too_early):
                return False
            self.click(x + 45, 430)
            print("点击小福袋位置，关闭福袋详情")
            self.operation.delay(1)
            if needswitch:
                self.swipe(760, 1600, 760, 800, 200)
                print("抽奖倒计时窗口不合适（剩余{}秒），不参与，上划切换直播间".format(lastsecond))
                stats['swipe_times'] += 1
                self.operation.delay(5)
                return True
            if too_late:
                wait_seconds = 60
                print("开奖倒计时只剩{}秒（少于{}秒）来不及参与，等待{}秒后再看".format(
                    lastsecond, self.min_countdown_seconds, wait_seconds))
            else:
                # 等到倒计时落进窗口内再回来看；单次最多等 5 分钟，避免长时间不响应
                wait_seconds = int(min(max(lastsecond - 60 * max_countdown_minutes + 5, 30), 300))
                print("开奖倒计时还有{}秒（超过{}分钟），暂不参与，等待{}秒后再看".format(
                    lastsecond, max_countdown_minutes, wait_seconds))
            self.operation.delay(wait_seconds)
            return True

        def handle_failed_participation(stats):
            """处理参与失败的情况"""
            self.swipe(760, 1600, 760, 800, 200)
            print("参与抽奖失败，上划切换直播间")
            stats['swipe_times'] += 1
            self.operation.delay(5)

        def handle_after_draw(x, stats, needswitch):
            """处理抽奖后的情况"""
            have_clicked_no_award = handle_lucky_draw(x)
            if have_clicked_no_award:
                if not needswitch:
                    self.operation.random_delay(180, 420)
                    return False
                else:
                    self.swipe(760, 1600, 760, 800, 200)
                    print("完成本次抽奖，上划切换直播间")
                    stats['swipe_times'] += 1
                    self.operation.delay(5)
                    return True
            return False

        # 主循环
        stats = initialize()
        # 上限为 0 表示不判定上限（沿用入口传入的 wait_minutes）
        effective_max_minutes = self.max_countdown_minutes if self.max_countdown_minutes > 0 else wait_minutes
        print("倒计时窗口：剩余少于{}秒不参与；剩余达到{}分钟先不参与（needswitch={}）".format(
            self.min_countdown_seconds, effective_max_minutes, needswitch))
        while True:
            self.deal_battery_level()
            current_hour = self.operation.get_current_hour()
            # 检查各种情况
            if handle_night_time(current_hour):
                continue
            x = self.check_have_fudai()
            if handle_no_fudai_time():
                continue
            # 处理福袋点击
            if handle_fudai_click(x, stats):
                # 获取福袋信息
                fudai_content_text, time_text = get_fudai_info()
            elif needswitch:
                handle_switch_live_room(stats)
                continue
            elif handle_closed_live_room(stats):
                continue
            elif handle_live_room_list(stats):
                continue
            elif handle_wait_time(stats):
                continue
            else:
                # 本轮在直播间里没找到福袋：计数 +1，供 handle_wait_time 决定何时换房间
                stats['wait_times'] += 1
                print("直播间暂无福袋，等待60S（本轮无福袋计数 {}/{}）".format(
                    stats['wait_times'],
                    self.no_fudai_switch_rounds if self.no_fudai_switch_rounds > 0 else '不限'))
                self.operation.delay(60)
                continue
            # 处理福袋内容
            if handle_fudai_content(x, stats, fudai_content_text, current_hour):
                continue
            # 处理倒计时
            countdown_result = handle_countdown(x, stats, time_text)
            if countdown_result is None:
                continue
                
            lastsecond, future_timestamp = countdown_result
            
            # 检查倒计时时间
            if handle_countdown_check(x, stats, lastsecond, needswitch, effective_max_minutes):
                continue
                
            # 参与抽奖
            if not self.attend_choujiang():
                handle_failed_participation(stats)
                continue
                
            # 等待开奖
            print(f"等待{lastsecond+5}秒后开奖")
            self.operation.delay(lastsecond + 5)
            
            # 处理开奖结果和后续操作
            if handle_after_draw(x, stats, needswitch):
                continue
            elif handle_closed_live_room(stats):
                continue
            elif handle_live_room_list(stats):
                continue


if __name__ == '__main__':
    douyin = fudai_analyse()






