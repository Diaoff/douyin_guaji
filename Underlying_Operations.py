from paddleocr import PaddleOCR
import subprocess
from datetime import datetime, timedelta
import os
import time
from PIL import Image
import numpy as np
import logging
import re
import random
import json

class underlying_operations:
    """底层操作类：封装 adb 命令、截图、OCR 与配置读写"""

    # ---- adb 执行策略 ----
    ADB = 'adb'              # adb 可执行文件（单元测试时可替换）
    ADB_TIMEOUT = 30         # 单条 adb 命令默认超时（秒）
    ADB_LONG_TIMEOUT = 90    # 截图/传输/启动类命令的超时（秒）
    ADB_RETRIES = 3          # 只读命令的尝试次数：瞬时失败（超时/返回码非0）可重试
    MUTATING_RETRIES = 1     # 有副作用命令（点击/滑动/输入/启停应用）只尝试1次，避免重复操作

    def __init__(self):
        self.ocr = PaddleOCR()
        logging.disable(logging.DEBUG)
        logging.disable(logging.WARNING)

    # ------------------------------------------------------------------
    # adb 统一执行入口
    # ------------------------------------------------------------------
    def _adb(self, args, device_id=None, timeout=None, retries=1):
        """执行一条 adb 命令。

        与旧实现的区别：
        - 以列表形式调用、不经过 shell：不再依赖 cmd.exe/findstr，也不再有引号与注入问题
        - 带超时：adb 卡死时不会让整个挂机流程僵住
        - 检查返回码：命令失败会打印原因，而不是静默继续
        Args:
            args: adb 子命令参数列表（不含 'adb' 与 '-s <device>'）
            device_id: 指定设备序列号；None/'' 表示不指定
            timeout: 单次超时秒数，默认 ADB_TIMEOUT
            retries: 尝试次数（含首次）。只读命令用 ADB_RETRIES，有副作用命令用 MUTATING_RETRIES
        Returns:
            (ok: bool, output: str)：ok 为 True 表示命令成功且返回码为0
        """
        if timeout is None:
            timeout = self.ADB_TIMEOUT
        attempts = max(1, int(retries))
        cmd = [self.ADB]
        if device_id:
            cmd += ['-s', str(device_id)]
        cmd += [str(arg) for arg in args]

        last_error = ''
        for attempt in range(1, attempts + 1):
            try:
                completed = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                last_error = '命令超时({}秒)'.format(timeout)
            except FileNotFoundError:
                # adb 不存在属于环境配置问题，重试没有意义，直接给出可操作的提示
                print("未找到 adb 命令，请确认已安装 Android Platform Tools 并加入 PATH")
                return False, ''
            except OSError as e:
                last_error = '执行失败: {}'.format(e)
            else:
                stdout = completed.stdout.decode('utf-8', errors='replace') if completed.stdout else ''
                stderr = completed.stderr.decode('utf-8', errors='replace') if completed.stderr else ''
                if completed.returncode == 0:
                    return True, stdout
                last_error = '返回码{}: {}'.format(completed.returncode, (stderr or stdout).strip()[:200])

            if attempt < attempts:
                print("adb 命令失败({})，第{}次重试: adb {}".format(
                    last_error, attempt, ' '.join(cmd[1:])))
                time.sleep(min(2 ** attempt, 5))

        print("adb 命令最终失败: adb {} -> {}".format(' '.join(cmd[1:]), last_error))
        return False, ''

    @staticmethod
    def _quote_for_device_shell(text):
        """按设备端 sh 的规则加引号：保留空格，并把单引号转义为 '\\'' 。

        这对 config.json 里 "I can't stop watching this!" 这类含单引号的评论是必要的，
        否则设备端 sh 会在单引号处截断字符串。
        """
        return "'" + text.replace("'", "'\\''") + "'"

    def select_device(self):
        """选择需要连接的设备"""
        ok, output = self._adb(['devices'], retries=self.ADB_RETRIES)
        if not ok:
            print("执行 adb devices 失败,请检查 adb 是否可用!")
            return False
        pattern = r'(\b(?:[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?::[0-9]+)?|[A-Za-z0-9]{8,})\b)\s*device\b'
        devicelist = re.findall(pattern, output)
        devicenum = len(devicelist)
        if devicenum == 0:
            print("当前无设备连接电脑,请检查设备连接情况!")
            return False
        elif devicenum == 1:
            print("当前有一台设备连接，编号:%s." % devicelist[0])
            return devicelist[0]
        else:
            print("当前存在多台设备连接! 输入数字选择对应设备:")
            dictdevice = {}
            for i in range(devicenum):
                ok_model, model_output = self._adb(
                    ['shell', 'getprop', 'ro.product.device'],
                    device_id=devicelist[i], retries=self.ADB_RETRIES)
                modestring = model_output.strip() if ok_model else '型号读取失败'
                print("%s:%s---%s" % (i + 1, devicelist[i], modestring))
                dictdevice[i + 1] = devicelist[i]
            # 旧实现对非数字输入会直接抛 ValueError 退出
            while True:
                raw = input().strip()
                if raw.isdigit() and int(raw) in dictdevice:
                    return dictdevice[int(raw)]
                print('输入不正确，请重新输入：')

    def _capture_screenshot(self, device_id, local_path, attempts=2):
        """截图并拉取到指定本地路径。

        截图与拉取属于一个整体动作，这里用「外层有界尝试 + 内层不重试」，
        避免嵌套重试把等待时间成倍放大（设备掉线时单次最长 ADB_LONG_TIMEOUT）。
        Returns:
            bool: 是否成功把截图拉到本地
        """
        for attempt in range(1, max(1, attempts) + 1):
            ok, _ = self._adb(['shell', 'screencap', '-p', '/sdcard/DCIM/screenshot.png'],
                              device_id=device_id, timeout=self.ADB_LONG_TIMEOUT, retries=1)
            if ok:
                ok, _ = self._adb(['pull', '/sdcard/DCIM/screenshot.png', local_path],
                                  device_id=device_id, timeout=self.ADB_LONG_TIMEOUT, retries=1)
            if ok:
                return True
            print("第{}次截图/拉取失败".format(attempt))
            self.delay(1)
        return False

    def get_screenshot(self, device_id, path='pic'):
        """截图并拉取到本地。

        修复点：旧实现在异常分支里调用 self.get_screenshot(path)，
        把路径当成了 device_id（参数错位），并会无限递归。
        这里改为有界重试，并如实返回是否成功。
        """
        if path == 'pic':
            save_dir = os.path.dirname(__file__) + '/pic'
        else:
            save_dir = os.path.dirname(__file__) + '/target_pic'
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        if self._capture_screenshot(device_id, save_dir):
            timetag = datetime.now().strftime('%H:%M:%S')
            print("{} 获取屏幕截图".format(timetag))
            return True
        print("获取屏幕截图失败,请检查设备连接情况!")
        return False

    def get_current_hour(self):
        """获取当前的时间小时数"""
        time_hour = datetime.now().strftime('%H')
        print("当前时间{}点".format(time_hour))
        return int(time_hour)

    def click_confirm(self, device_id):
        """点击确认键"""
        self._adb(['shell', 'input', 'keyevent', '66'], device_id=device_id,
                  retries=self.MUTATING_RETRIES)
        print("点击确认键")

    def click_back(self, device_id):
        """点击返回键"""
        self._adb(['shell', 'input', 'keyevent', '4'], device_id=device_id,
                  retries=self.MUTATING_RETRIES)
        print("点击返回键")

    def get_ballery_level(self, device_id):
        """获取设备电量信息

        Returns:
            int 电量百分比；读取或解析失败时返回 None（调用方需处理）
        """
        ok, output = self._adb(['shell', 'dumpsys', 'battery'], device_id=device_id,
                               retries=self.ADB_RETRIES)
        if not ok:
            print("读取设备电量失败")
            return None
        # 旧实现按字符下标扫描到换行符，输出格式变化就会 IndexError；这里改用正则
        match = re.search(r'level:\s*(\d+)', output)
        if not match:
            print("未能从 dumpsys battery 输出中解析出电量")
            return None
        battery_level = int(match.group(1))
        print("设备当前电量为{}".format(battery_level))
        return battery_level

    def get_device_resolution(self, device_id):
        """获取设备分辨率"""
        ok, output = self._adb(['shell', 'wm', 'size'], device_id=device_id,
                               retries=self.ADB_RETRIES)
        match = re.search(r'(?:Physical|Override) size:\s*(\d+)x(\d+)', output) if ok else None
        if not match:
            raise ValueError("无法从 adb wm size 输出中解析分辨率: {!r}".format(output[:200]))
        width = int(match.group(1))
        height = int(match.group(2))
        print("设备分辨率为：{}*{}".format(width, height))
        return width, height

    def save_reward_pic(self, device_id):
        """保存中奖截图"""
        path = os.path.dirname(__file__) + '/pic'
        if not os.path.exists(path):
            os.makedirs(path)
        timepic = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        if self._capture_screenshot(device_id, '{}/{}.png'.format(path, timepic)):
            self._adb(['shell', 'rm', '/sdcard/DCIM/screenshot.png'], device_id=device_id,
                      retries=self.MUTATING_RETRIES)
            print("中奖了，点击领奖，保存中奖图片{}.png".format(timepic))
            return True
        print("保存中奖截图失败")
        return False

    def cut_pic(self, left_up=(0, 63), right_down=(1080, 1620), target='', name=''):
        '''裁剪图片，获取需要的区域小图片方便识别'''
        if target == '' or target == False:
            path = os.path.dirname(__file__) + '/pic'
            pic1_path = path + '/screenshot.png'
            pic = Image.open(pic1_path)
            if name == '':
                cut_pic_path = path + '/cut.png'
            else:
                cut_pic_path = path + '/' + name + '.png'
            pic.crop((left_up[0], left_up[1], right_down[0], right_down[1])).save(cut_pic_path)
            return True
        path_target = os.path.dirname(__file__) + '/pic/' + target
        pic1_path = path_target + '/screenshot.png'
        pic = Image.open(pic1_path)
        if name == '':
            cut_pic_path = path_target + '/cut.png'
        else:
            cut_pic_path = path_target + '/' + name + '.png'
        pic.crop((left_up[0], left_up[1], right_down[0], right_down[1])).save(cut_pic_path)
        return True

    def analyse_pic_word(self, picname='', change_color=0):
        """识别图像中的文字, change_color=1或2为不同的二值化模式，其他不做处理"""
        path = os.path.dirname(__file__) + '/pic'
        if picname == '':
            pic = path + '/cut.png'
        else:
            pic = path + '/' + picname + '.png'
        img = Image.open(pic)
        img = img.convert('L')  # 转换为灰度图
        if change_color == 1:
            img = img.point(lambda x: 0 if x < 128 else 255)  # 二值化
        elif change_color == 2:
            img = img.point(lambda x: 0 if x < 180 else 255)  # 二值化
        # img.save(pic)
        # img.show()
        img_np = np.array(img)  # 将 Image 对象转换为 numpy 数组
        result = self.ocr.ocr(img_np)
        # OCR 在异常/空图时可能返回 None、[None] 或 [[]]，统一由 extract_ocr_content 兜底
        return self.extract_ocr_content(result)

    def extract_ocr_content(self, content=None):
        """对OCR识别到的内容进行取值和拼接，变成完整的一段内容

        旧实现只判定了 [None]，当 PaddleOCR 返回 None 时会抛
        TypeError: 'NoneType' object is not subscriptable。
        """
        if not content or not content[0]:
            return ''
        extracted_content = []
        for item in content[0]:  # item 的结构为 [位置信息, (识别内容, 置信度)]
            try:
                extracted_content.append(item[1][0])
            except (IndexError, TypeError):
                continue
        contains = ''.join(context for context in extracted_content if context)
        # print(contain)
        return contains

    def delay(self, seconds=1):
        time.sleep(seconds)

    def random_delay(self, min_seconds=1, max_seconds=100):
        """等待随机的时长"""
        random_wait_seconds = random.randint(min_seconds, max_seconds)
        print("随机等待{}秒.".format(random_wait_seconds))
        self.delay(random_wait_seconds)

    def check_countdown(self, last_time=''):
        """对倒计时时间进行转化，变成秒存储"""
        try:
            last_time = ''.join([char for char in last_time if char.isdigit()])
            if len(last_time) == 4:
                # minutes, seconds = map(int, last_time.split(':'))
                minutes = int(last_time[:2])
                seconds = int(last_time[2:])
            else:
                print("时间格式异常")
                return False
            # 转换为总秒数
            total_seconds = minutes * 60 + seconds
            print("剩余总秒数：", total_seconds)
            if total_seconds > 900:  # 如果识别到的分钟大于15，说明识别异常了，按15分钟处理
                total_seconds = 890
            # datetime.strptime(in_time, '%H:%M')
            # time_obj = datetime.strptime(in_time, '%H:%M')
            # print("转换后的时间格式：", time_obj)
            now = datetime.now()
            future_time = now + timedelta(seconds=total_seconds)
            # 将到期时间转换为时间戳
            future_timestamp = future_time.timestamp()
            future_datetime = datetime.fromtimestamp(future_timestamp)
            # 将datetime对象格式化为通常的时间格式
            formatted_future_time = future_datetime.strftime('%Y-%m-%d %H:%M:%S')
            print("预计开奖时间：", formatted_future_time)
            return total_seconds, future_timestamp
        except ValueError:
            print("输入的字符串不是有效的时间格式")
            return False

    def swipe(self, device_id, left_up_x=0, left_up_y=0, right_down_x=1080, right_down_y=1500, steps=200):
        """划动屏幕"""
        self._adb(
            ['shell', 'input', 'swipe', left_up_x, left_up_y, right_down_x, right_down_y, steps],
            device_id=device_id, retries=self.MUTATING_RETRIES)

    def click(self, device_id, x=500, y=500):
        """点击坐标位置"""
        self._adb(['shell', 'input', 'tap', x, y], device_id=device_id,
                  retries=self.MUTATING_RETRIES)

    def input_text(self, device_id: str, text: str):
        """输入文本"""
        if not text:
            return False
        # 不经 shell 调用 adb，因此只需为设备端 sh 加引号
        ok, _ = self._adb(['shell', 'input', 'text', self._quote_for_device_shell(text)],
                          device_id=device_id, retries=self.MUTATING_RETRIES)
        if not ok:
            print("输入文本失败，改用备用内容")
            ok, _ = self._adb(['shell', 'input', 'text', 'good!'], device_id=device_id,
                              retries=self.MUTATING_RETRIES)
        return ok

    def get_douyin_package_name(self, device_id):
        """获取抖音app的包名

        旧实现依赖 Windows 的 findstr，在 Linux/macOS 上必然失败；这里改为纯 Python 过滤。
        """
        ok, output = self._adb(['shell', 'pm', 'list', 'packages'], device_id=device_id,
                               retries=self.ADB_RETRIES)
        if not ok:
            print("获取包名失败: pm list packages 执行异常")
            return None
        candidates = []
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith('package:'):
                continue
            package_name = line.split(':', 1)[1].strip()
            if 'aweme' in package_name:
                candidates.append(package_name)
        if not candidates:
            print("未在设备上找到抖音(aweme)相关包名")
            return None
        # 优先主包(抖音)，其次极速版等其它 aweme 包
        if 'com.ss.android.ugc.aweme' in candidates:
            return 'com.ss.android.ugc.aweme'
        return sorted(candidates)[0]

    def get_douyin_activity_name(self, device_id, package_name):
        """获取抖音的启动 Activity"""
        if not package_name:
            return None
        # 优先使用 resolve-activity，输出干净且与系统版本无关
        ok, output = self._adb(
            ['shell', 'cmd', 'package', 'resolve-activity', '--brief', package_name],
            device_id=device_id, retries=self.ADB_RETRIES)
        if ok:
            for line in reversed(output.splitlines()):
                line = line.strip()
                if '/' in line and 'priority' not in line:
                    return line
        # 老设备兜底：解析 dumpsys package
        ok, output = self._adb(['shell', 'dumpsys', 'package', package_name],
                               device_id=device_id, timeout=self.ADB_LONG_TIMEOUT,
                               retries=self.ADB_RETRIES)
        if not ok:
            print("获取活动名失败")
            return None
        for line in output.splitlines():
            if 'android.intent.action.MAIN' in line and package_name in line:
                for token in line.replace('{', ' ').replace('}', ' ').split():
                    if token.startswith(package_name + '/'):
                        return token
        print("未能解析出抖音的启动 Activity")
        return None

    def start_douyin_app(self, device_id):
        """启动抖音：优先 am start -n <组件>，失败时用 monkey 兜底"""
        package_name = self.get_douyin_package_name(device_id)
        if not package_name:
            print("启动抖音失败: 未找到抖音包名")
            return False
        component = self.get_douyin_activity_name(device_id, package_name)
        if component:
            ok, output = self._adb(['shell', 'am', 'start', '-n', component],
                                   device_id=device_id, timeout=self.ADB_LONG_TIMEOUT,
                                   retries=self.MUTATING_RETRIES)
            # am start 即使组件不存在也可能返回0，因此同时检查输出
            if ok and 'Error' not in output and 'does not exist' not in output:
                print("[{}] 已在设备 {} 启动: {}".format(
                    datetime.now().strftime('%Y-%m-%d  %H:%M:%S'), device_id, package_name))
                self.delay(3)
                return True
            print("am start 启动失败，改用 monkey 兜底")
        ok, _ = self._adb(
            ['shell', 'monkey', '-p', package_name, '-c', 'android.intent.category.LAUNCHER', '1'],
            device_id=device_id, timeout=self.ADB_LONG_TIMEOUT, retries=self.MUTATING_RETRIES)
        if ok:
            print("[{}] 已通过 monkey 在设备 {} 启动: {}".format(
                datetime.now().strftime('%Y-%m-%d  %H:%M:%S'), device_id, package_name))
            self.delay(3)
            return True
        print("启动抖音失败: am start 与 monkey 均未成功")
        return False

    def stop_douyin_app(self, device_id):
        """强制关闭抖音

        修复点：旧实现是 subprocess.run(cmd) 直接传字符串且未加 shell=True，
        Windows 上会抛 FileNotFoundError 并被 except 吞掉 —— 抖音实际上从未被关闭，
        夜间停机/重启复位都会静默失效。
        """
        package_name = self.get_douyin_package_name(device_id)
        if not package_name:
            print("关闭抖音失败: 未找到抖音包名")
            return False
        ok, _ = self._adb(['shell', 'am', 'force-stop', package_name],
                          device_id=device_id, timeout=self.ADB_LONG_TIMEOUT,
                          retries=self.MUTATING_RETRIES)
        if ok:
            print("[{}] 已在设备 {} 关闭: {}".format(
                datetime.now().strftime('%Y-%m-%d  %H:%M:%S'), device_id, package_name))
            self.delay(2)
            return True
        print("关闭抖音失败")
        return False

    @staticmethod
    def _resolve_config_path(config_path):
        """把相对路径的配置文件解析到脚本所在目录。

        config.json 原先按相对路径读取，即相对于「当前工作目录」。只要不是先 cd 到项目目录
        再运行（双击、快捷方式、用绝对路径调用等），就会静默读不到配置，
        导致 contains_want / contains_not_want 等关键词列表全为空、筛选逻辑失效。
        """
        if os.path.isabs(config_path) or os.path.exists(config_path):
            return config_path
        beside_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), config_path)
        if os.path.exists(beside_script):
            return beside_script
        return config_path

    def load_config(self, config_path="config.json", key="contains_want", default=None):
        """从配置文件中加载数据

        文件缺失或解析失败时不再静默返回空列表，而是给出提示并返回默认值。
        """
        if default is None:
            default = []
        config_path = self._resolve_config_path(config_path)
        if not os.path.exists(config_path):
            print("未找到配置文件{}，{} 使用默认值".format(config_path, key))
            return default
        try:
            with open(config_path, "r", encoding="utf-8") as file:
                config = json.load(file)
        except (OSError, ValueError) as e:
            print("配置文件{}读取失败({})，{} 使用默认值".format(config_path, e, key))
            return default
        return config.get(key, default)


    def update_config_with_offsets(self, config_path="config.json", device_offsets=None):
        """
        更新设备偏移值到配置文件
        仅新增未写入过的设备偏移值，不覆盖已存在的记录。
        """
        if device_offsets is None:
            device_offsets = {}
        config_path = self._resolve_config_path(config_path)

        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as file:
                try:
                    config = json.load(file)
                except Exception:
                    config = {}
        else:
            config = {}

        existing_map = config.get('device_offsets', {})
        # 仅合并未存在的条目
        new_entries = {device_id: offset for device_id, offset in device_offsets.items() if device_id not in existing_map}
        if not new_entries:
            return False

        existing_map.update(new_entries)
        config['device_offsets'] = existing_map
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        return True
