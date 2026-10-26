"""冒烟验证：确认依赖链可跑通 + 三个 P0 崩溃修复的行为级回归。

运行方式（在项目根目录）：
    .venv\\Scripts\\python.exe tests\\smoke_test.py

不需要连接手机；只验证「能导入、能构造、OCR 可用、修复生效」。
为不在仓库根目录留下 .log / pic 产物，测试内部会切换到临时目录。
"""
import ast
import inspect
import os
import sys
import tempfile
import textwrap
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.chdir(tempfile.gettempdir())

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


# ---------- 1. 依赖链可导入 ----------
import numpy
import PIL
import paddle
import paddleocr

check("import paddle", True, f"paddle {paddle.__version__}")
check("import paddleocr", True, f"paddleocr {paddleocr.__version__}")
check("import numpy", True, f"numpy {numpy.__version__}")
check("import PIL", True, f"pillow {PIL.__version__}")

# ---------- 2. 项目模块可导入（真实入口链） ----------
from douyin_fudai import fudai_analyse, Tee  # noqa: E402
import douyin_guaji  # noqa: E402

check("import douyin_fudai / douyin_guaji", True)

# ---------- 3. 真实构造（含 PaddleOCR 初始化 + 日志文件） ----------
analyser = fudai_analyse()
check("fudai_analyse() 构造成功", True)
check("日志文件已创建且未关闭",
      os.path.exists(analyser.log_file.name) and not analyser.log_file.closed,
      os.path.basename(analyser.log_file.name))
check("stdout 已被 Tee 接管", isinstance(sys.stdout, Tee))

# ---------- 4. OCR 真实识别（走 analyse_pic_word 的同一底层路径） ----------
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
import numpy as np  # noqa: E402

img = Image.new("RGB", (460, 130), "white")
draw = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 72)
except OSError:
    font = ImageFont.load_default()
draw.text((20, 25), "没有抽中", font=font, fill="black")

ocr_result = analyser.operation.ocr.ocr(np.array(img.convert("L")))
text = analyser.operation.extract_ocr_content(ocr_result)
check("OCR 识别中文", "没有抽中" in text, f"识别结果={text!r}")
check("extract_ocr_content 对空结果健壮", analyser.operation.extract_ocr_content([[]]) == "")

# ---------- 5. P0-1 回归：顶部坐标异常不再崩溃 ----------
got = analyser.get_fudai_contain_and_countdown(None)
check("get_fudai_contain_and_countdown(None) 不再 TypeError", got == ("", ""), f"返回={got!r}")
got = analyser.get_fudai_contain_and_countdown("1000")
check("get_fudai_contain_and_countdown('1000') 不再 TypeError", got == ("", ""), f"返回={got!r}")
got = analyser.get_fudai_contain_and_countdown(-5)
check("get_fudai_contain_and_countdown(-5) 被拦截", got == ("", ""), f"返回={got!r}")

# ---------- 6. P0-3 回归：日志句柄可关闭且 stdout 还原 ----------
tee_ref = sys.stdout
analyser.close_log()
check("close_log() 关闭句柄", analyser.log_file.closed)
check("close_log() 还原 stdout", sys.stdout is analyser.original_stdout)
try:
    analyser.close_log()  # 幂等
    check("close_log() 可重复调用", True)
except Exception as e:  # pragma: no cover
    check("close_log() 可重复调用", False, repr(e))
try:
    tee_ref.write("关闭后写入 Tee 不应抛异常\n")  # Tee.write 的 closed 保护
    check("Tee.write 对已关闭文件健壮", True)
except Exception as e:  # pragma: no cover
    check("Tee.write 对已关闭文件健壮", False, repr(e))

# ---------- 7. 纯逻辑回归 ----------
check("check_countdown('1230') == 750s", analyser.operation.check_countdown("1230")[0] == 750)
check("check_countdown('12:30') 兼容冒号", analyser.operation.check_countdown("12:30")[0] == 750)
check("check_countdown 异常格式返回 False", analyser.operation.check_countdown("abc") is False)

analyser.contains_want = ["鱼竿"]
analyser.contains_not_want = ["钓鱼毛巾"]
check("check_contain 命中不想要 -> True", analyser.check_contain("钓鱼毛巾", 12) is True)
check("check_contain 命中想要 -> False", analyser.check_contain("碳素鱼竿", 12) is False)
check("check_contain 未明确 -> False", analyser.check_contain("随便什么", 12) is False)
check("check_contain 半夜不做要求 -> False", analyser.check_contain("钓鱼毛巾", 3) is False)

# ---------- 8. P0-2 回归：重试改为循环实现 ----------


def calls_itself(func, attr_name):
    # 注意：这里必须用 textwrap.dedent，inspect.cleandoc 以正文行计算缩进边距，
    # 会把方法体的缩进一并剥掉，导致 ast.parse 报 IndentationError。
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == attr_name
               for n in ast.walk(tree))


guaji_src = ast.parse(textwrap.dedent(inspect.getsource(douyin_guaji.fudai_guaji.guaji)))
check("入口 guaji 无自递归", not calls_itself(douyin_guaji.fudai_guaji.guaji, "guaji"))
check("入口 guaji 为 while 循环", any(isinstance(n, ast.While) for n in ast.walk(guaji_src)))
check("restart_and_reset 无自递归", not calls_itself(analyser.restart_and_reset, "restart_and_reset"))
check("restart_and_reset 有重试上限", "max_retries" in inspect.signature(analyser.restart_and_reset).parameters)
check("Underlying_Operations 死代码已清理",
      "__del__" not in open(os.path.join(REPO_ROOT, "Underlying_Operations.py"), encoding="utf-8").read())

# ---------- 9. P1 回归：adb 调用加固 ----------
import subprocess as sp  # noqa: E402
from unittest import mock  # noqa: E402

ops = analyser.operation
uo_path = os.path.join(REPO_ROOT, "Underlying_Operations.py")
uo_src = open(uo_path, encoding="utf-8").read()

# 9.1 静态：不再依赖 shell / 平台专属命令 / 裸 except
def uses_shell_true(path):
    """AST 级检查：只认真正的 subprocess 调用，注释里的字样不算"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    return any(
        kw.arg == 'shell' and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for node in ast.walk(tree) if isinstance(node, ast.Call)
        for kw in node.keywords)


def has_bare_except(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return any(isinstance(node, ast.ExceptHandler) and node.type is None
               for node in ast.walk(tree))


check("不再使用 os.system", "os.system" not in uo_src)
check("不再使用 shell=True", not uses_shell_true(uo_path))
check("不再使用 findstr", "| findstr" not in uo_src and 'findstr "' not in uo_src)
check("不再有裸 except:", not has_bare_except(uo_path))


def capture_adb(call, outputs=None, returncode=0):
    """拦截 subprocess.run，记录最后一次 adb 调用的 argv，并返回被调用函数的返回值"""
    outputs = outputs or {}
    box = {}

    def fake_run(cmd, **kwargs):
        box['cmd'] = [str(c) for c in cmd]
        box['kwargs'] = kwargs
        joined = ' '.join(box['cmd'])
        # 取「最长匹配」的键：避免 screen_brightness 与 screen_brightness_mode 这类前缀互相误命中
        matched = [key for key in outputs if key in joined]
        stdout = outputs[max(matched, key=len)].encode() if matched else b''
        return sp.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=b'')

    with mock.patch('subprocess.run', fake_run), \
            mock.patch.object(ops, 'delay', lambda *a, **k: None):
        returned = call()
    return box.get('cmd', []), box.get('kwargs', {}), returned


# 9.2 真实子进程：列表形式执行 + 返回码判定 + 超时（用当前解释器冒充 adb 二进制）
real_adb = ops.ADB
try:
    ops.ADB = sys.executable
    ok, out = ops._adb(['-c', 'print("hello-adb")'])
    check("真实子进程执行并捕获stdout", ok and out.strip() == "hello-adb", f"out={out.strip()!r}")
    ok, _ = ops._adb(['-c', 'import sys; sys.exit(3)'])
    check("返回码非0判定为失败", ok is False)
    started = time.time()
    ok, _ = ops._adb(['-c', 'import time; time.sleep(10)'], timeout=1)
    check("超时被拦截且不卡死", ok is False and time.time() - started < 8,
          f"耗时{time.time() - started:.1f}s")
finally:
    ops.ADB = real_adb

# 9.3 adb 缺失 / 重试语义
missing = {'n': 0}


def no_adb(cmd, **kwargs):
    missing['n'] += 1
    raise FileNotFoundError(2, 'No such file')


with mock.patch('subprocess.run', no_adb):
    ok, _ = ops._adb(['devices'], retries=3)
check("adb缺失时明确失败且不重试", ok is False and missing['n'] == 1, f"尝试{missing['n']}次")

flaky = {'n': 0}


def flaky_run(cmd, **kwargs):
    flaky['n'] += 1
    ok_now = flaky['n'] >= 3
    return sp.CompletedProcess(args=cmd, returncode=0 if ok_now else 1,
                               stdout=b'ok' if ok_now else b'', stderr=b'boom')


with mock.patch('subprocess.run', flaky_run), mock.patch('time.sleep', lambda *a: None):
    ok, out = ops._adb(['shell', 'dumpsys', 'battery'], device_id='dev1', retries=ops.ADB_RETRIES)
check("只读命令可重试至成功", ok and flaky['n'] == 3, f"尝试{flaky['n']}次")

failing = {'n': 0}


def always_fail(cmd, **kwargs):
    failing['n'] += 1
    return sp.CompletedProcess(args=cmd, returncode=1, stdout=b'', stderr=b'x')


with mock.patch('subprocess.run', always_fail):
    ok, _ = ops._adb(['shell', 'input', 'tap', 1, 2], device_id='dev1',
                     retries=ops.MUTATING_RETRIES)
check("有副作用命令不重试（避免重复点击）", ok is False and failing['n'] == 1, f"尝试{failing['n']}次")

# 9.4 argv 构造
cmd, kwargs, _ = capture_adb(lambda: ops.click('dev1', 540, 1200))
check("click 使用列表形式且不经shell",
      cmd == ['adb', '-s', 'dev1', 'shell', 'input', 'tap', '540', '1200'] and 'shell' not in kwargs,
      f"{cmd}")

cmd, _, _ = capture_adb(lambda: ops.swipe('dev1', 1, 2, 3, 4, 200))
check("swipe 参数顺序正确",
      cmd == ['adb', '-s', 'dev1', 'shell', 'input', 'swipe', '1', '2', '3', '4', '200'], f"{cmd}")

cmd, _, _ = capture_adb(lambda: ops.click_back('dev1'))
check("click_back 使用 keyevent 4", cmd == ['adb', '-s', 'dev1', 'shell', 'input', 'keyevent', '4'],
      f"{cmd}")

cmd, _, _ = capture_adb(lambda: ops.input_text('dev1', "I can't stop watching this!"))
check("input_text 单引号按设备端sh规则转义",
      cmd[-1] == "'I can'\\''t stop watching this!'", f"{cmd[-1]!r}")
check("设备端引号转义函数", ops._quote_for_device_shell("a'b c") == "'a'\\''b c'")

cmd, _, _ = capture_adb(lambda: ops.get_douyin_package_name('dev1'))
check("取包名不再依赖findstr", cmd[-3:] == ['pm', 'list', 'packages'], f"{cmd}")

_, _, pkg = capture_adb(
    lambda: ops.get_douyin_package_name('dev1'),
    outputs={'pm list packages': 'package:com.ss.android.ugc.aweme.lite\n'
                                 'package:com.ss.android.ugc.aweme\n'})
check("包名优先主包而非极速版", pkg == 'com.ss.android.ugc.aweme', f"{pkg!r}")

# 9.5 静默失效修复：stop_douyin_app 真的下发 force-stop
pkg_outputs = {'pm list packages': 'package:com.ss.android.ugc.aweme\npackage:com.other.app\n',
               'resolve-activity': 'com.ss.android.ugc.aweme/.splash.SplashActivity\n'}
cmd, _, _ = capture_adb(lambda: ops.stop_douyin_app('dev1'), outputs=pkg_outputs)
check("stop_douyin_app 真正下发 force-stop",
      cmd == ['adb', '-s', 'dev1', 'shell', 'am', 'force-stop', 'com.ss.android.ugc.aweme'], f"{cmd}")

cmd, _, _ = capture_adb(lambda: ops.start_douyin_app('dev1'), outputs=pkg_outputs)
check("start_douyin_app 使用 am start -n",
      cmd == ['adb', '-s', 'dev1', 'shell', 'am', 'start', '-n',
              'com.ss.android.ugc.aweme/.splash.SplashActivity'], f"{cmd}")

cmd, _, _ = capture_adb(
    lambda: ops.start_douyin_app('dev1'),
    outputs={'pm list packages': 'package:com.ss.android.ugc.aweme\n',
             'resolve-activity': 'Error: Activity class does not exist\n'})
check("am start 失败时回退 monkey 兜底", cmd[:6] == ['adb', '-s', 'dev1', 'shell', 'monkey', '-p'],
      f"{cmd}")

# 9.5b 截图失败时有界重试，不因嵌套重试把等待时间成倍放大
shot_calls = {'n': 0}


def shot_fail(cmd, **kwargs):
    shot_calls['n'] += 1
    return sp.CompletedProcess(args=cmd, returncode=1, stdout=b'', stderr=b'x')


with mock.patch('subprocess.run', shot_fail), \
        mock.patch.object(ops, 'delay', lambda *a, **k: None), \
        mock.patch('os.makedirs', lambda *a, **k: None):
    ok = ops.get_screenshot('dev1')
check("截图失败时有界重试（不嵌套放大）", ok is False and shot_calls['n'] == 2,
      f"adb调用{shot_calls['n']}次")

# 9.6 时间窗常量与旧行为一致
check("in_hour_range 闭区间",
      analyser.in_hour_range(1, 1, 7) and analyser.in_hour_range(7, 1, 7)
      and not analyser.in_hour_range(0, 1, 7) and not analyser.in_hour_range(8, 1, 7))
check("in_hour_range 支持跨零点",
      analyser.in_hour_range(23, 22, 6) and analyser.in_hour_range(3, 22, 6)
      and not analyser.in_hour_range(12, 22, 6))
check("时间窗与旧硬编码一致",
      analyser.in_hour_range(7, analyser.NIGHT_STOP_START, analyser.NIGHT_STOP_END) is True
      and analyser.in_hour_range(7, analyser.NO_SWITCH_START, analyser.NO_SWITCH_END) is False
      and analyser.in_hour_range(0, analyser.NO_SWITCH_START, analyser.NO_SWITCH_END) is True
      and analyser.WAKE_HOUR == analyser.NIGHT_STOP_END + 1)

# 9.7 电量与配置
check("停止充电设备号来自config.json",
      isinstance(analyser.charging_notice_devices, list))

with mock.patch.object(ops, 'get_ballery_level', lambda *a: None):
    try:
        analyser.deal_battery_level()
        check("电量读取失败不再 TypeError", True)
    except Exception as e:  # pragma: no cover
        check("电量读取失败不再 TypeError", False, repr(e))

battery_seq = iter([10, 100])
with mock.patch.object(ops, 'get_ballery_level', lambda *a: next(battery_seq)), \
        mock.patch.object(analyser, 'back_to_zhibo_list', lambda *a: None), \
        mock.patch.object(ops, 'delay', lambda *a, **k: None):
    analyser.deal_battery_level()
check("低电量进入等待后电量恢复可继续", True)

# ---------- 10. P2 回归：死代码清理 / 像素扫描向量化 / 版本对齐 ----------
import io  # noqa: E402
import douyin_fudai as df_module  # noqa: E402
import Underlying_Operations as uo_module  # noqa: E402

# 10.1 死代码已删除
fudai_src = open(os.path.join(REPO_ROOT, "douyin_fudai.py"), encoding="utf-8").read()
defined = {n.name for n in ast.walk(ast.parse(fudai_src)) if isinstance(n, ast.FunctionDef)}
check("死代码 deal_robot_pic 已删除", "deal_robot_pic" not in defined)
check("死代码 deal_robot_pic_change_color 已删除", "deal_robot_pic_change_color" not in defined)

# 10.2 版本号单一来源且与 README 一致
readme_text = open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8").read()
check("代码内版本号为 V4.4", df_module.VERSION == "V4.4", df_module.VERSION)
check("README 当前版本与代码一致", df_module.VERSION in readme_text)

# 10.3 向量化后的滑动距离与原算法一致
#     用合成图 + 临时重定向 __file__：既覆盖真实裁剪链路，又不在仓库里留产物
workdir = os.path.join(tempfile.gettempdir(), "dsh_robot_pic_test")
os.makedirs(os.path.join(workdir, "pic"), exist_ok=True)
screen_path = os.path.join(workdir, "pic", "screenshot.png")


def make_screen(white=True, dark=True):
    im = Image.new("RGB", (1080, 2400), (150, 150, 150))
    draw = ImageDraw.Draw(im)
    if white:
        draw.rectangle([173, 984, 175, 986], fill=(255, 255, 255))  # 裁剪区局部 (30, 100)
    if dark:
        draw.rectangle([323, 984, 325, 986], fill=(0, 0, 60))       # 裁剪区局部 (180, 100)
    im.save(screen_path)


with mock.patch.object(df_module, "__file__", os.path.join(workdir, "douyin_fudai.py")), \
        mock.patch.object(uo_module, "__file__", os.path.join(workdir, "Underlying_Operations.py")):
    make_screen()
    got = analyser.check_robot_pic_distance()
    check("向量化滑动距离=150（与旧算法一致）", got == 150, f"distance={got}")
    make_screen(white=False)
    got = analyser.check_robot_pic_distance()
    check("无白点时返回 None（调用方回退默认距离）", got is None, f"distance={got}")
    make_screen(dark=False)
    got = analyser.check_robot_pic_distance()
    check("同排无缺口时返回 None", got is None, f"distance={got}")

# 10.4 README V4.4 声称的 GBK 编码报错已被真正兜底
check("stdout 已启用不可编码字符兜底",
      getattr(analyser.original_stdout, "errors", None) == "replace",
      f"errors={getattr(analyser.original_stdout, 'errors', None)}")
gbk_buf = io.BytesIO()
gbk_stream = io.TextIOWrapper(gbk_buf, encoding="gbk", errors="replace")
try:
    gbk_stream.write("中文测试🎉")  # GBK 无法编码 🎉
    gbk_stream.flush()
    check("GBK 控制台下不可编码字符不再抛异常", True)
except UnicodeEncodeError as e:  # pragma: no cover
    check("GBK 控制台下不可编码字符不再抛异常", False, repr(e))

# ---------- 11. find_y_offset 的 OCR 去重（带开关）----------
off_dir = os.path.join(tempfile.gettempdir(), "dsh_offset_test")
os.makedirs(os.path.join(off_dir, "pic"), exist_ok=True)
Image.new("RGB", (1080, 2400), (250, 250, 250)).save(os.path.join(off_dir, "pic", "screenshot.png"))
# 渲染一张与真实裁剪同尺寸 (170x42) 的"说点什么"，用于验证 OCR 可重复性这一去重前提
probe_img = Image.new("RGB", (170, 42), (250, 250, 250))
ImageDraw.Draw(probe_img).text((3, 5), "说点什么...",
                               font=ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 24),
                               fill=(150, 150, 150))
probe_img.save(os.path.join(off_dir, "pic", "say_something.png"))

with mock.patch.object(uo_module, "__file__", os.path.join(off_dir, "Underlying_Operations.py")), \
        mock.patch.object(df_module, "__file__", os.path.join(off_dir, "douyin_fudai.py")):
    # 去重的前提：同一张裁剪图 + 同一模式，OCR 结果必须可重复
    r_first = ops.analyse_pic_word("say_something", 0)
    r_more = [ops.analyse_pic_word("say_something", 0) for _ in range(3)]
    check("OCR 对同一张图可重复（去重前提成立）", all(r == r_first for r in r_more), f"{r_first!r}")
    check("去重后仍能识别到'说点'", "说点" in r_first, f"{r_first!r}")

    def count_find_y_offset_calls(retry_times):
        """最坏情况（全部不匹配）下统计 OCR 调用次数"""
        counter = {'n': 0}

        def fake_analyse(*args, **kwargs):
            counter['n'] += 1
            return ''

        original_analyse = ops.analyse_pic_word
        saved = (ops.delay, ops.get_screenshot, analyser.device_id,
                 analyser.device_offsets, analyser.ocr_retry_times)
        ops.analyse_pic_word = fake_analyse
        ops.delay = lambda *a, **k: None
        ops.get_screenshot = lambda *a, **k: True
        analyser.device_id = 'offset-bench'
        analyser.device_offsets = {}
        analyser.ocr_retry_times = retry_times
        try:
            returned = analyser.find_y_offset()
        finally:
            ops.analyse_pic_word = original_analyse
            (ops.delay, ops.get_screenshot, analyser.device_id,
             analyser.device_offsets, analyser.ocr_retry_times) = saved
        return counter['n'], returned

    calls_1, result_1 = count_find_y_offset_calls(1)
    calls_2, result_2 = count_find_y_offset_calls(2)
    check("去重后最坏情况 OCR 调用=102（旧实现为 208）", calls_1 == 102, f"调用{calls_1}次")
    check("开关 ocr_retry_times=2 可恢复重复识别", calls_2 == 204, f"调用{calls_2}次")
    check("两种设置返回一致", result_1 == result_2, f"{result_1} vs {result_2}")

check("默认 ocr_retry_times=1", analyser.ocr_retry_times == 1, str(analyser.ocr_retry_times))
check("config.json 暴露 ocr_retry_times 开关",
      "ocr_retry_times" in open(os.path.join(REPO_ROOT, "config.json"), encoding="utf-8").read())

# 11.1 配置文件必须按「脚本所在目录」解析：
#      本测试开头已 chdir 到临时目录，若仍按相对路径读取，这里就会读不到内容。
#      注意这里直接重新 load_config（而不是读 analyser.contains_want），
#      因为第 7 节已经把实例属性覆盖成单元素列表了。
loaded_want = ops.load_config("config.json", "contains_want")
loaded_not_want = ops.load_config("config.json", "contains_not_want")
check("换工作目录后仍能读到 config.json（关键词非空）",
      len(loaded_want) > 0 and len(loaded_not_want) > 0 and "鱼竿" in loaded_want,
      f"want={len(loaded_want)} not_want={len(loaded_not_want)}")
check("配置文件解析到脚本所在目录而非工作目录",
      os.path.dirname(os.path.abspath(ops._resolve_config_path("config.json"))) == REPO_ROOT,
      ops._resolve_config_path("config.json"))

# ---------- 12. 无福袋换房间计数 + 倒计时窗口解耦 ----------
fudai_tree = ast.parse(fudai_src)

# 12.1 静态防护：防止 wait_times 的计数再次丢失
has_wait_increment = any(
    isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript)
    and isinstance(node.target.slice, ast.Constant) and node.target.slice.value == 'wait_times'
    for node in ast.walk(fudai_tree))
check("主循环里存在 wait_times 自增（防止再次丢失计数）", has_wait_increment)

cd_func = [n for n in ast.walk(fudai_tree)
           if isinstance(n, ast.FunctionDef) and n.name == 'handle_countdown_check'][0]
cd_assigns = {n.targets[0].id: n.value for n in ast.walk(cd_func)
              if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
check("倒计时窗口判定已与 needswitch 解耦",
      'too_late' in cd_assigns and 'too_early' in cd_assigns
      and 'needswitch' not in ast.dump(cd_assigns['too_late'])
      and 'needswitch' not in ast.dump(cd_assigns['too_early']))

# 12.2 配置项默认值
check("默认 no_fudai_switch_rounds=4", analyser.no_fudai_switch_rounds == 4,
      str(analyser.no_fudai_switch_rounds))
check("默认 min_countdown_seconds=15", analyser.min_countdown_seconds == 15,
      str(analyser.min_countdown_seconds))
check("默认 max_countdown_minutes=15", analyser.max_countdown_minutes == 15,
      str(analyser.max_countdown_minutes))


class _LoopEscape(Exception):
    """用于从 fudai_choujiang 的死循环里跳出来"""


def drive_main_loop(*, have_fudai=False, time_text='', switch_rounds=4,
                    min_seconds=15, max_minutes=15, needswitch=False, max_rounds=6,
                    attend_result=None):
    """驱动真实的 fudai_choujiang 主循环，返回观察到的事件。

    have_fudai=False 时每轮都判定不到福袋（验证换房间计数）；
    have_fudai=True 时每轮都找到福袋，并按 time_text 走倒计时判定。
    attend_result=None 时一到参与抽奖就跳出（验证是否参与）；
    传 True/False 则让它继续往下走到等待开奖与开奖结果处理。
    跳出点：轮次上限 / 换房间(swipe) / 参与抽奖(attend_choujiang)。
    """
    events = {'rounds': 0, 'swipes': 0, 'delays': [], 'participated': False}
    saved = []

    def patch(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def fake_check_have_fudai():
        events['rounds'] += 1
        if events['rounds'] > max_rounds:
            raise _LoopEscape()
        return 300 if have_fudai else False

    def fake_swipe(*args, **kwargs):
        events['swipes'] += 1
        raise _LoopEscape()

    def fake_delay(*args, **kwargs):
        events['delays'].append(args[0] if args else 0)

    def fake_attend(*args, **kwargs):
        events['participated'] = True
        if attend_result is None:
            raise _LoopEscape()
        return attend_result

    patch(ops, 'delay', fake_delay)
    patch(ops, 'random_delay', lambda *a, **k: None)   # 开奖后那段 3~7 分钟等待不能真等
    patch(ops, 'get_screenshot', lambda *a, **k: True)
    patch(ops, 'get_current_hour', lambda: 12)      # 固定白天，避开夜间停机
    patch(ops, 'click', lambda *a, **k: None)
    patch(ops, 'get_device_resolution', lambda *a, **k: (1080, 2400))
    patch(analyser, 'find_y_offset', lambda *a, **k: 0)
    # 屏蔽亮度调整，避免驱动测试真的去连 adb（亮度由第 13 节单独覆盖）
    patch(analyser, 'apply_screen_brightness', lambda *a, **k: False)
    patch(analyser, 'check_have_fudai', fake_check_have_fudai)
    patch(analyser, 'swipe', fake_swipe)
    patch(analyser, 'deal_battery_level', lambda *a, **k: None)
    patch(analyser, 'check_zhibo_is_closed', lambda *a, **k: False)
    patch(analyser, 'check_in_zhibo_list', lambda *a, **k: False)
    patch(analyser, 'find_fudai_detail_top_y', lambda *a, **k: 1000)
    patch(analyser, 'get_fudai_contain_and_countdown', lambda top_y=1000: ('鱼竿大奖', time_text))
    patch(analyser, 'check_contain', lambda *a, **k: False)   # 内容想要 -> 继续
    patch(analyser, 'attend_choujiang', fake_attend)
    patch(analyser, 'no_fudai_switch_rounds', switch_rounds)
    patch(analyser, 'min_countdown_seconds', min_seconds)
    patch(analyser, 'max_countdown_minutes', max_minutes)
    saved.append((analyser, 'last_find_fudai_time', analyser.last_find_fudai_time))
    analyser.last_find_fudai_time = time.time()
    try:
        analyser.fudai_choujiang('loop-test', needswitch, 15)
    except _LoopEscape:
        pass
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
    return events


# 12.3 连续 2 轮无福袋 -> 第 3 轮换房间（旧实现永不触发）
ev = drive_main_loop(have_fudai=False, switch_rounds=2)
check("连续2轮无福袋后第3轮主动换房间", ev['swipes'] == 1 and ev['rounds'] == 3,
      f"轮次={ev['rounds']} 换房间={ev['swipes']}")

# 12.4 开关设为 0 -> 恢复"一直等到房间关闭"
ev = drive_main_loop(have_fudai=False, switch_rounds=0, max_rounds=6)
check("no_fudai_switch_rounds=0 时不主动换房间", ev['swipes'] == 0 and ev['rounds'] == 7,
      f"轮次={ev['rounds']} 换房间={ev['swipes']}")

# 12.5 倒计时窗口：默认模式下"只剩几秒就不参与"现在真的生效
ev = drive_main_loop(have_fudai=True, time_text='0342')
check("窗口内(222秒)会参与抽奖", ev['participated'], f"participated={ev['participated']}")

ev = drive_main_loop(have_fudai=True, time_text='0008')
check("只剩8秒时不参与（旧实现会照样参与）",
      not ev['participated'] and 60 in ev['delays'],
      f"participated={ev['participated']} delays={ev['delays']}")

ev = drive_main_loop(have_fudai=True, time_text='1000', max_minutes=5)
check("上限设5分钟、剩10分钟时不参与并等待",
      not ev['participated'] and 300 in ev['delays'],
      f"participated={ev['participated']} delays={ev['delays']}")

# 12.6 已知交互：读数超过900秒会被压到890秒，所以上限=15分钟几乎无法触发
ev = drive_main_loop(have_fudai=True, time_text='2000', max_minutes=15)
check("上限15分钟时 20:00 被压到890秒 -> 仍会参与（已知交互）", ev['participated'],
      f"participated={ev['participated']}")

# 12.7 回归：needswitch=True 时仍是"关掉详情后上划换直播间"的原行为
ev = drive_main_loop(have_fudai=True, time_text='0008', needswitch=True)
check("换房间模式：只剩8秒时上划换直播间（原行为）",
      ev['swipes'] == 1 and not ev['participated'],
      f"swipes={ev['swipes']} participated={ev['participated']}")

ev = drive_main_loop(have_fudai=True, time_text='1000', max_minutes=5, needswitch=True)
check("换房间模式：剩10分钟（上限5分钟）时上划换直播间",
      ev['swipes'] == 1 and not ev['participated'],
      f"swipes={ev['swipes']} participated={ev['participated']}")

ev = drive_main_loop(have_fudai=True, time_text='0342', needswitch=True)
check("换房间模式：窗口内仍会参与", ev['participated'], f"participated={ev['participated']}")

# ---------- 13. 运行时降低屏幕亮度省电 ----------


def capture_all_adb(call, outputs=None, fail_when=None):
    """记录调用期间所有 adb argv；fail_when 命中时让该条命令返回非0"""
    outputs = outputs or {}
    recorded = []

    def fake_run(cmd, **kwargs):
        argv = [str(c) for c in cmd]
        joined = ' '.join(argv)
        recorded.append(joined)
        returncode_here = 1 if (fail_when and fail_when in joined) else 0
        # 取「最长匹配」的键：避免 screen_brightness 与 screen_brightness_mode 这类前缀互相误命中
        matched = [key for key in outputs if key in joined]
        stdout = outputs[max(matched, key=len)].encode() if matched else b''
        return sp.CompletedProcess(args=cmd, returncode=returncode_here, stdout=stdout, stderr=b'')

    with mock.patch('subprocess.run', fake_run), \
            mock.patch.object(ops, 'delay', lambda *a, **k: None):
        call()
    return recorded


bright_outputs = {'settings get system screen_brightness_mode': '1',
                  'settings get system screen_brightness': '128',
                  'reduce_bright_colors_activated': '0'}

# 13.1 正常路径：关自动亮度 -> cmd display -> 记录原值
ops._brightness_backup = None
calls = capture_all_adb(lambda: ops.set_screen_brightness('dev1', 20), outputs=bright_outputs)
check("亮度：先关闭自动亮度", any('settings put system screen_brightness_mode 0' in c for c in calls),
      str(calls))
check("亮度：优先用 cmd display set-brightness 0.2000",
      any('cmd display set-brightness 0.2000' in c for c in calls))
check("亮度：已记录原亮度用于还原",
      ops._brightness_backup and ops._brightness_backup['brightness'] == '128'
      and ops._brightness_backup['used_cmd'] is True,
      str(ops._brightness_backup))
check("亮度：检查了极暗模式", any('reduce_bright_colors_activated' in c for c in calls))

# 13.2 还原：写回原亮度与自动亮度模式
calls = capture_all_adb(lambda: ops.restore_screen_brightness())
check("还原亮度：cmd display reset-brightness", any('cmd display reset-brightness' in c for c in calls))
check("还原亮度：写回原亮度128", any('settings put system screen_brightness 128' in c for c in calls))
check("还原亮度：写回自动亮度模式1",
      any('settings put system screen_brightness_mode 1' in c for c in calls))
check("还原亮度：备份已清空（可重复调用）", ops._brightness_backup is None)
check("还原亮度：无备份时直接返回", ops.restore_screen_brightness() is False)

# 13.3 降级路径：cmd display 不支持时退回 settings（20% -> 51/255）
ops._brightness_backup = None
calls = capture_all_adb(lambda: ops.set_screen_brightness('dev1', 20), outputs=bright_outputs,
                        fail_when='cmd display set-brightness')
check("亮度降级：cmd display 失败时退回 settings 写 51",
      any('settings put system screen_brightness 51' in c for c in calls), str(calls))
check("亮度降级：不标记 used_cmd", ops._brightness_backup['used_cmd'] is False)

# 13.4 还原时若用过 cmd display，需要 reset
calls = capture_all_adb(lambda: ops.restore_screen_brightness())
check("降级路径还原：不调用 cmd display reset-brightness",
      not any('reset-brightness' in c for c in calls))

# 13.5 配置项与开关
check("默认 screen_brightness=20", analyser.screen_brightness == 20, str(analyser.screen_brightness))
saved_device_id, saved_brightness = analyser.device_id, analyser.screen_brightness
analyser.device_id, analyser.screen_brightness = 'dev1', 0
ops._brightness_backup = None
calls = capture_all_adb(lambda: analyser.apply_screen_brightness(), outputs=bright_outputs)
check("screen_brightness=0 表示不调整（不发任何指令）",
      analyser.apply_screen_brightness() is False and not any('screen_brightness' in c for c in calls),
      str(calls))
analyser.device_id, analyser.screen_brightness = saved_device_id, saved_brightness

# 13.6 开启时会注册退出还原（避免测试进程真的去连 adb）
ops._brightness_backup = None
analyser.device_id = 'dev1'


def _brightness_stub(cmd, **kwargs):
    joined = ' '.join(str(c) for c in cmd)
    out = b'1' if joined.endswith('screen_brightness_mode') else b'128'
    return sp.CompletedProcess(args=cmd, returncode=0, stdout=out, stderr=b'')


with mock.patch('atexit.register') as reg, mock.patch('subprocess.run', _brightness_stub):
    applied = analyser.apply_screen_brightness()
check("开启亮度调整时会注册退出还原", applied and reg.called)
analyser.device_id = saved_device_id
ops._brightness_backup = None

# 13.7 极暗模式会被提示（只提醒不改设置）
ops._brightness_backup = None
calls = capture_all_adb(lambda: ops.set_screen_brightness('dev1', 30),
                        outputs=dict(bright_outputs, **{'reduce_bright_colors_activated': '1'}))
check("极暗模式下仍会完成亮度设置（只提示不阻断）",
      any('cmd display set-brightness 0.3000' in c for c in calls)
      and ops._brightness_backup is not None)
ops._brightness_backup = None

# ---------- 14. 多房间排队：时间窗与冲突裁决 ----------
from fudai_queue import FudaiTask, FudaiTaskQueue  # noqa: E402

# 14.1 时间窗与基本流程
q = FudaiTaskQueue(arrive_margin=60, result_margin=40)
t = FudaiTask('房间A', '鱼竿', draw_ts=1000, created_ts=900)
accepted, reason = q.add(t)
check("入队：无冲突时有保障", accepted and t.guaranteed, reason)
check("时间窗 = [开奖-arrive, 开奖+result]", t.window_start == 940 and t.window_end == 1040,
      f"{t.window_start}~{t.window_end}")
check("未到回房窗口时不该出发", q.due(900) is None)
check("到达回房窗口时选中该任务", q.due(940) is t)

# 14.2 核心：开奖时间相近（差30秒）怎么裁决
q2 = FudaiTaskQueue(arrive_margin=60, result_margin=40, conflict_policy='earlier_first')
a = FudaiTask('房间A', '鱼竿', draw_ts=1000, created_ts=900)
b = FudaiTask('房间B', '鱼竿', draw_ts=1030, created_ts=900)   # 只差30秒 -> 窗口重叠
q2.add(a)
ok_b, reason_b = q2.add(b)
check("相近开奖：先开奖的A保持有保障", a.guaranteed is True)
check("相近开奖：后开奖的B降级为伺机而不是丢弃",
      ok_b and b.guaranteed is False and b.state == 'pending', reason_b)
check("相近开奖：B带上了降级原因", '降级' in b.reason, b.reason)
check("相近开奖：到点只去A", q2.due(940) is a)
q2.mark_serving(a)
q2.finish(a, 'done')
missed = q2.purge_expired(1071)          # B 的窗口 end = 1030+40 = 1070
check("A服务完后B已错过 -> 标记missed", b.state == 'missed' and b in missed)
check("错过的任务可事后去消息中心补领", b in q2.missed_tasks())

# 14.3 后加入的更早开奖，应该反过来取代
q3 = FudaiTaskQueue(arrive_margin=60, result_margin=40)
late = FudaiTask('房间B', 'x', draw_ts=1030, created_ts=900)
early = FudaiTask('房间A', 'y', draw_ts=1000, created_ts=900)
q3.add(late)
q3.add(early)
check("后加入但开奖更早的应取得保障权", early.guaranteed and not late.guaranteed)

# 14.4 valuable_first：价值高的优先，哪怕开奖更晚
q4 = FudaiTaskQueue(arrive_margin=60, result_margin=40, conflict_policy='valuable_first')
plain = FudaiTask('房间A', '普通奖品', draw_ts=1000, created_ts=900, value=1)
wanted = FudaiTask('房间B', '想要的鱼竿', draw_ts=1030, created_ts=900, value=2)
q4.add(plain)
q4.add(wanted)
check("valuable_first：命中想要的优先（可晚开奖）", wanted.guaranteed and not plain.guaranteed)

# 14.5 skip_new：窗口冲突干脆不参与
q5 = FudaiTaskQueue(arrive_margin=60, result_margin=40, conflict_policy='skip_new')
q5.add(FudaiTask('房间A', 'x', draw_ts=1000, created_ts=900))
ok5, reason5 = q5.add(FudaiTask('房间B', 'y', draw_ts=1030, created_ts=900))
check("skip_new：窗口冲突时不参与", ok5 is False and 'skip_new' in reason5, reason5)

# 14.6 队列上限与伺机任务的安全余量
q6 = FudaiTaskQueue(max_pending=1)
q6.add(FudaiTask('房间A', 'x', draw_ts=1000, created_ts=900))
ok6, reason6 = q6.add(FudaiTask('房间B', 'y', draw_ts=9000, created_ts=900))
check("队列达上限时不再接单", ok6 is False and '队列已满' in reason6, reason6)

q7 = FudaiTaskQueue(arrive_margin=60, result_margin=40, travel_budget=45)
guard = FudaiTask('房间G', 'x', draw_ts=1590, created_ts=1500)   # 有保障，窗口1530开始
q7.add(guard)
spare = FudaiTask('房间S', 'y', draw_ts=1510, created_ts=1400)
spare.guaranteed = False                                        # 手动摆一个伺机任务
spare.window_start, spare.window_end = q7._window(spare.draw_ts)
q7.tasks.append(spare)
check("伺机任务可能耽误下一个有保障任务时就不去", q7.due(1500) is None)

# 14.7 房名归一化
check("房名归一化：去标点空格表情",
      analyser.room_key_from_title("小明【直播中】 🎉") == "小明直播中",
      analyser.room_key_from_title("小明【直播中】 🎉"))
check("房名归一化：截断到8个字符",
      len(analyser.room_key_from_title("一二三四五六七八九十")) == 8)
check("房名归一化：空输入返回空", analyser.room_key_from_title("") == "")

# 14.8 带坐标的 OCR（供"按名找房"使用）
# 注意：这里用自己的图，不能复用 11.1 的 say_something.png ——
# 11.3 节会调用真实的 find_y_offset，从空白 screenshot 裁剪并覆盖那个文件名。
boxes_img = Image.new("RGB", (170, 42), (250, 250, 250))
ImageDraw.Draw(boxes_img).text((3, 5), "说点什么...",
                               font=ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 24),
                               fill=(150, 150, 150))
boxes_img.save(os.path.join(off_dir, "pic", "boxes_probe.png"))
with mock.patch.object(uo_module, "__file__", os.path.join(off_dir, "Underlying_Operations.py")):
    boxes = ops.analyse_pic_word_with_boxes("boxes_probe")
check("boxes：能拿到文本与中心坐标",
      bool(boxes) and any('说点' in item['text'] for item in boxes), str(boxes))
check("boxes：坐标落在裁剪图范围内",
      all(0 <= item['x'] <= 170 and 0 <= item['y'] <= 42 for item in boxes), str(boxes))
with mock.patch.object(ops, '_load_pic_for_ocr', lambda *a, **k: np.zeros((10, 10))), \
        mock.patch.object(ops.ocr, 'ocr', lambda img: [None]):
    check("boxes：OCR 返回 [None] 时得空列表", ops.analyse_pic_word_with_boxes('x') == [])
with mock.patch.object(ops, '_load_pic_for_ocr', lambda *a, **k: np.zeros((10, 10))), \
        mock.patch.object(ops.ocr, 'ocr', lambda img: None):
    check("boxes：OCR 返回 None 时得空列表", ops.analyse_pic_word_with_boxes('x') == [])


def run_find_room(screens, room_key='小明', max_scrolls=2):
    """用伪造的逐屏 OCR 结果驱动 find_room_in_follow_list"""
    result = {'clicks': 0, 'scrolls': 0}
    queue_screens = list(screens)
    saved = (ops.get_screenshot, ops.analyse_pic_word_with_boxes, ops.click, ops.delay,
             analyser.swipe, analyser.cut_pic)

    def fake_boxes(*args, **kwargs):
        return queue_screens.pop(0) if queue_screens else []

    ops.get_screenshot = lambda *a, **k: True
    ops.analyse_pic_word_with_boxes = fake_boxes
    ops.click = lambda *a, **k: result.__setitem__('clicks', result['clicks'] + 1)
    ops.delay = lambda *a, **k: None
    analyser.swipe = lambda *a, **k: result.__setitem__('scrolls', result['scrolls'] + 1)
    analyser.cut_pic = lambda *a, **k: True
    try:
        found = analyser.find_room_in_follow_list(room_key, max_scrolls=max_scrolls)
    finally:
        (ops.get_screenshot, ops.analyse_pic_word_with_boxes, ops.click, ops.delay,
         analyser.swipe, analyser.cut_pic) = saved
    return found, result


found, stat = run_find_room([[{'text': '别人家', 'x': 100, 'y': 300, 'score': 0.9},
                             {'text': '小明直播中', 'x': 540, 'y': 800, 'score': 0.9}]])
check("按名找房：命中即点击", found and stat['clicks'] == 1 and stat['scrolls'] == 0, str(stat))
found, stat = run_find_room([[], [{'text': '小明', 'x': 200, 'y': 500, 'score': 0.9}]], max_scrolls=2)
check("按名找房：首屏没有则滚动后命中", found and stat['clicks'] == 1 and stat['scrolls'] == 1,
      str(stat))
found, stat = run_find_room([[], []], max_scrolls=1)
check("按名找房：找不到时返回False且滚动次数受限", found is False and stat['scrolls'] == 1,
      str(stat))

# 14.9 配置项默认值（实验性功能默认关闭）
check("多房间功能默认关闭", analyser.multi_room_enabled is False)
check("队列参数已从config读取",
      analyser.arrive_margin_seconds == 60 and analyser.result_margin_seconds == 40
      and analyser.conflict_policy == 'earlier_first' and analyser.max_pending_fudai == 5,
      f"{analyser.arrive_margin_seconds}/{analyser.result_margin_seconds}/"
      f"{analyser.conflict_policy}/{analyser.max_pending_fudai}")

# ---------- 15. 悬浮窗：日志总线、状态快照、倒计时与降级 ----------
from fudai_status import (LogBus, FudaiStatus, format_countdown,  # noqa: E402
                          format_snapshot_lines)
import fudai_overlay as overlay_module  # noqa: E402

# 15.1 LogBus：print 的写入不保证整行到达，要能跨块拼接
check("LogBus：容量有最小值保护（防止把环形缓冲设得过小）", LogBus(max_lines=1).max_lines == 10,
      str(LogBus(max_lines=1).max_lines))
bus = LogBus(max_lines=12)
bus.write('第一行\n第二')
bus.write('行\n')
bus.write('半行没有换行')
# 还没遇到换行的半行不应该出现在缓冲区里
check("LogBus：未结束的半行先攒着不显示", bus.tail(10) == ['第一行', '第二行'], str(bus.tail(10)))
bus.write('\n')          # 补上换行，半行才落库
check("LogBus：补换行后落库", bus.tail(10) == ['第一行', '第二行', '半行没有换行'],
      str(bus.tail(10)))
for i in range(20):
    bus.write('第{}条\n'.format(i))
check("LogBus：环形缓冲不超过上限", len(bus.tail(100)) == 12, str(len(bus.tail(100))))
check("LogBus：tail 取的是最后N行", bus.tail(2) == ['第18条', '第19条'], str(bus.tail(2)))
bus.clear()
check("LogBus：可清空", bus.tail(5) == [])

# 15.2 FudaiStatus：倒计时在查询时刻计算（注入 now_fn 保证可测）
fake_now = {'t': 1000.0}
status = FudaiStatus(now_fn=lambda: fake_now['t'])
check("状态：初始没有在等的福袋", status.snapshot()['remaining'] is None)
status.set_draw(1060, prize='鱼竿')
check("状态：剩余秒数=开奖-现在", status.snapshot()['remaining'] == 60,
      str(status.snapshot()['remaining']))
fake_now['t'] = 1100.0
check("状态：时间推进后剩余归零而不是负数", status.snapshot()['remaining'] == 0)
status.clear_draw()
check("状态：清空后回到没有在等", status.snapshot()['remaining'] is None)
fake_now['t'] = 1200.0
status.set_pending([{'room': '房间A', 'prize': 'x', 'draw_ts': 1260},
                    {'room': '房间B', 'prize': 'y', 'draw_ts': 1320}])
check("状态：队列里每个任务各自算剩余时间",
      [item['remaining'] for item in status.snapshot()['pending']] == [60, 120],
      str(status.snapshot()['pending']))
status.set_stats(done=3, missed=1)
check("状态：统计可更新",
      status.snapshot()['stats']['done'] == 3 and status.snapshot()['stats']['missed'] == 1)
snapshot_copy = status.snapshot()
snapshot_copy['phase'] = '被外部改了'
check("状态：快照是副本，改动不影响内部", status.snapshot()['phase'] != '被外部改了')

# 15.3 倒计时格式化
check("倒计时格式化：None -> --:--", format_countdown(None) == '--:--')
check("倒计时格式化：0 -> 00:00", format_countdown(0) == '00:00')
check("倒计时格式化：65 -> 01:05", format_countdown(65) == '01:05')
check("倒计时格式化：3661 -> 1:01:01", format_countdown(3661) == '1:01:01')
check("倒计时格式化：负数按0处理", format_countdown(-5) == '00:00')

# 15.4 悬浮窗文本行
lines = format_snapshot_lines(status.snapshot())
check("文本行：默认包含倒计时", any(line.startswith('开奖倒计时') for line in lines), str(lines))
check("文本行：可省略倒计时（悬浮窗单独大字显示）",
      not any(line.startswith('开奖倒计时')
              for line in format_snapshot_lines(status.snapshot(), include_countdown=False)))
check("文本行：显示排队任务", any('排队中 2 个' in line for line in lines), str(lines))
check("文本行：未识别房名时不显示房间行",
      not any(line.startswith('当前房间') for line in lines), str(lines))

# 15.5 Tee 与日志总线真的连通
tee_bus = LogBus()
Tee(io.StringIO(), bus=tee_bus).write('喂给总线的内容\n')
check("Tee：写入同时进总线", tee_bus.tail(1) == ['喂给总线的内容'], str(tee_bus.tail(1)))
check("运行过程中确实有日志进了总线（启动阶段已连通）",
      len(analyser.log_bus.tail(400)) > 0, str(len(analyser.log_bus.tail(400))))

# 15.6 悬浮窗：无图形环境必须安全降级，有图形环境则真建一次
probe_status = FudaiStatus()
probe_bus = LogBus()
with mock.patch.object(overlay_module, 'tk', None):
    check("没有 tkinter 时 create_overlay 返回 None 而不是抛异常",
          overlay_module.create_overlay(probe_status, probe_bus) is None)

overlay = overlay_module.create_overlay(probe_status, probe_bus,
                                        alpha=0.8, log_lines=4, poll_ms=100000)
if overlay is None:
    check("悬浮窗：无图形环境，跳过实际创建", True, "无显示环境")
else:
    try:
        probe_bus.write('悬浮窗内日志1\n悬浮窗内日志2\n')
        probe_status.set_draw(time.time() + 90, prize='测试奖品')
        probe_status.set_phase('单元测试中')
        overlay.update_once()
        countdown_text = overlay.countdown_label.cget('text')
        log_text = overlay.log_label.cget('text')
        info_text = overlay.info_label.cget('text')
        check("悬浮窗：窗口可创建", bool(overlay.root.winfo_exists()))
        check("悬浮窗：倒计时控件是 mm:ss", len(countdown_text) == 5 and ':' in countdown_text,
              countdown_text)
        check("悬浮窗：日志控件显示日志", '悬浮窗内日志1' in log_text, repr(log_text))
        check("悬浮窗：信息区显示奖品与状态",
              '测试奖品' in info_text and '单元测试中' in info_text, repr(info_text))
        check("悬浮窗：整窗半透明已设置",
              abs(float(overlay.root.attributes('-alpha')) - 0.8) < 0.01,
              str(overlay.root.attributes('-alpha')))
        check("悬浮窗：无边框且置顶",
              bool(overlay.root.overrideredirect()) and bool(overlay.root.attributes('-topmost')))
    finally:
        overlay.close()
    check("悬浮窗：可正常关闭", True)

# 15.7 与主循环联动：真的等到开奖时才登记倒计时
analyser.status.clear_draw()
drive_main_loop(have_fudai=True, time_text='0342')
snap_waited = analyser.status.snapshot()
check("主循环联动：等到开奖时登记倒计时与奖品",
      snap_waited['draw_ts'] > 0 and snap_waited['prize'] == '鱼竿大奖',
      str({key: snap_waited[key] for key in ('draw_ts', 'prize', 'phase')}))
check("主循环联动：阶段已上报", bool(snap_waited['phase']), snap_waited['phase'])
analyser.status.clear_draw()
drive_main_loop(have_fudai=True, time_text='0008')      # 倒计时太短被拦下
check("主循环联动：被倒计时窗口拦下时不登记倒计时",
      analyser.status.snapshot()['draw_ts'] == 0,
      str(analyser.status.snapshot()['draw_ts']))

# ---------- 16. 福袋图标识别：纵向带扫描（修复"差1像素就漏"）----------
FUDAI_TEST_W, FUDAI_TEST_H = 1280, 2772      # 用户手机的真实分辨率
TARGET_PURPLE = (188, 186, 241)              # 落在 FUDAI_COLOR 范围内的颜色


def is_target_color(color):
    return (183 <= color[0] <= 193 and 172 <= color[1] <= 193 and 237 <= color[2] <= 248)


def build_screen(icon_px=None, size=(FUDAI_TEST_W, FUDAI_TEST_H)):
    """造一张背景截图，可选在指定本机像素位置画一小块目标色"""
    im = Image.new('RGB', size, (200, 200, 202))
    if icon_px:
        x, y = icon_px
        ImageDraw.Draw(im).rectangle([x, y, x + 4, y + 2], fill=TARGET_PURPLE)
    return im


saved_ratio = (analyser.resolution_ratio_x, analyser.resolution_ratio_y)
analyser.resolution_ratio_x, analyser.resolution_ratio_y = FUDAI_TEST_W, FUDAI_TEST_H

# 16.1 真实故障场景：旧逻辑只探 y=465，而图标紫色从 y=466 开始
screen_arr = np.asarray(build_screen((60, 466)).convert('RGB')).astype(np.int16)
probe_y = int(403 * FUDAI_TEST_H / 2400)                       # = 465
old_probe_hit = any(
    is_target_color(tuple(int(v) for v in screen_arr[probe_y, x]))
    for x in range(int(33 * FUDAI_TEST_W / 1080), int(52 * FUDAI_TEST_W / 1080)))
check("（对照）旧的单行探测在本场景确实漏掉", not old_probe_hit)
hit = analyser.find_fudai_icon(screen_arr)
check("纵向带扫描：图标从 y=466 起也能找到", hit is not None, str(hit))
# 画的是 x60-64/y466-468 的矩形，质心落在正中 (62,467)
check("纵向带扫描：命中点是图标色块的质心 (62,467)", hit == (62, 467), str(hit))
if hit:
    hit_color = tuple(int(v) for v in screen_arr[hit[1], hit[0]])
    check("返回的命中点本身就是目标色像素（保证点得准）",
          is_target_color(hit_color), str(hit_color))

# 16.2 搜索带边界（基准 403±35 -> 本机 y 424~505）
for label, icon_px, expect in [
        ("带内顶部 y=430", (60, 430), True),
        ("带内底部 y=500", (60, 500), True),
        ("带外偏下 y=520", (60, 520), False),
        ("带外偏上 y=410", (60, 410), False),
        ("带外偏右 x=600", (600, 466), False),
        ("带外偏左 x=20", (20, 466), False)]:
    case_arr = np.asarray(build_screen(icon_px).convert('RGB')).astype(np.int16)
    got = analyser.find_fudai_icon(case_arr)
    check("搜索带边界：{} -> {}".format(label, "找到" if expect else "不找"),
          (got is not None) is expect, str(got))

# 16.3 没有福袋时不能误报
check("纯背景截图不会误报",
      analyser.find_fudai_icon(np.asarray(build_screen(None).convert('RGB')).astype(np.int16))
      is None)

# 16.4 完整 check_have_fudai：返回基准 x，并把本机像素坐标记下来供点击
fudai_dir = os.path.join(tempfile.gettempdir(), 'dsh_fudai_hit_test')
os.makedirs(os.path.join(fudai_dir, 'pic'), exist_ok=True)
build_screen((60, 466)).save(os.path.join(fudai_dir, 'pic', 'screenshot.png'))
saved_16 = (analyser.device_id, analyser.device_offsets, analyser.fudai_hit)
analyser.device_id = 'fudai-hit-test'
analyser.device_offsets = {'fudai-hit-test': 0}     # 防止测试真的去写 config.json
analyser.fudai_hit = None
with mock.patch.object(df_module, '__file__', os.path.join(fudai_dir, 'douyin_fudai.py')), \
        mock.patch.object(uo_module, '__file__', os.path.join(fudai_dir, 'Underlying_Operations.py')), \
        mock.patch.object(ops, 'get_screenshot', lambda *a, **k: True), \
        mock.patch.object(ops, 'delay', lambda *a, **k: None):
    found_x = analyser.check_have_fudai()
check("check_have_fudai：带扫描后能返回命中的基准 x", found_x == 52, str(found_x))
check("check_have_fudai：记下的是本机像素坐标（点击不再来回换算）",
      analyser.fudai_hit == (62, 467), str(analyser.fudai_hit))
analyser.device_id, analyser.device_offsets, analyser.fudai_hit = saved_16
analyser.resolution_ratio_x, analyser.resolution_ratio_y = saved_ratio

# ---------- 17. 参与按钮：宽区域扫描 + 按坐标点击（修复"按钮文字没匹配上"）----------
analyser.resolution_ratio_x, analyser.resolution_ratio_y = 1280, 2772
ATTEND_ORIGIN_Y = 1860 * 2772 // 2400          # 按钮带在设备像素里的起点 y = 2148

# 17.1 read_attend_button：把裁剪区内的坐标换算成绝对设备像素
saved_17 = (analyser.cut_pic, ops.analyse_pic_word_with_boxes)
analyser.cut_pic = lambda *a, **k: True
ops.analyse_pic_word_with_boxes = lambda name, mode=0: [
    {'text': '参与抽奖', 'x': 100, 'y': 50, 'w': 200, 'h': 40, 'score': 0.9}]
band_text, band_boxes = analyser.read_attend_button()
check("按钮带：文本拼接正确", band_text == '参与抽奖', band_text)
check("按钮带：坐标换算成绝对设备像素",
      band_boxes[0]['x'] == 100 and band_boxes[0]['y'] == 50 + ATTEND_ORIGIN_Y,
      str(band_boxes[0]))
analyser.cut_pic, ops.analyse_pic_word_with_boxes = saved_17

# 17.2 attend_choujiang：按识别到的坐标点击，而不是固定位置
def run_attend(band_text, band_boxes, comment_ui=False):
    """驱动一次 attend_choujiang，记录点击坐标与输入内容"""
    record = {'clicks': [], 'inputs': []}
    saved = (ops.click, ops.get_screenshot, ops.delay, ops.input_text,
             analyser.read_attend_button, analyser.check_can_send_comment,
             ops.get_device_resolution)
    ops.click = lambda dev, x, y: record['clicks'].append((x, y))
    ops.get_screenshot = lambda *a, **k: True
    ops.delay = lambda *a, **k: None
    ops.input_text = lambda dev, text: record['inputs'].append(text)
    analyser.read_attend_button = lambda *a, **k: (band_text, list(band_boxes))
    analyser.check_can_send_comment = lambda: comment_ui
    try:
        record['result'] = analyser.attend_choujiang()
    finally:
        (ops.click, ops.get_screenshot, ops.delay, ops.input_text,
         analyser.read_attend_button, analyser.check_can_send_comment,
         ops.get_device_resolution) = saved
    return record


# 按钮在 y=2600（旧实现固定点 (500*1.185, 2060*1.155) ≈ (592,2379)，根本点不到这里）
rec = run_attend('参与抽奖', [{'text': '参与抽奖', 'x': 540, 'y': 2600, 'w': 320, 'h': 70,
                            'score': 0.9}])
check("参与按钮：按识别到的坐标点击（不再点固定位置）",
      rec['result'] is True and rec['clicks'] and rec['clicks'][0] == (540, 2600),
      str(rec['clicks'][:2]))

# 多个候选行时：按钮是短标签且在底部，任务描述/广告是长句（用真机实测数据）
rec = run_attend('短代！任务加入购物粉丝团已达成发送评论：右上角领券，1+2号合并付！到手318元未达成一键发表评论',
                 [{'text': '发送评论：右上角领券，1+2号合并付！到手318元', 'x': 518, 'y': 2374,
                   'w': 867, 'h': 60, 'score': 0.9},
                  {'text': '一键发表评论', 'x': 642, 'y': 2525, 'w': 292, 'h': 72, 'score': 0.9}])
check("参与按钮：长句描述不会被当成按钮（真机数据回归）",
      rec['clicks'] and rec['clicks'][0] == (642, 2525), str(rec['clicks'][:2]))

# "一键发表评论"这类任务：关键字命中的是"评论"，点击后若出现评论输入界面就发一条
rec = run_attend('一键发表评论', [{'text': '一键发表评论', 'x': 540, 'y': 2650, 'w': 360,
                              'h': 72, 'score': 0.9}], comment_ui=True)
check("一键发表评论：能识别并点击按钮",
      rec['result'] is True and rec['clicks'] and rec['clicks'][0] == (540, 2650),
      str(rec['clicks'][:2]))
check("一键发表评论：弹出评论界面时会真的发一条评论",
      len(rec['inputs']) == 1, str(rec['inputs']))
rec = run_attend('一键发表评论', [{'text': '一键发表评论', 'x': 540, 'y': 2650, 'w': 360,
                              'h': 72, 'score': 0.9}], comment_ui=False)
check("一键发表评论：没有评论界面时不乱输入",
      rec['inputs'] == [], str(rec['inputs']))

# 完全没匹配上时不能乱点，并且如实返回失败
rec = run_attend('领券，1+2号合并付！到手318', [{'text': '领券，1+2号合并付！到手318',
                                        'x': 600, 'y': 2400, 'w': 400, 'h': 60, 'score': 0.9}])
check("广告文字不会误触发参与（如实返回失败且不点击）",
      rec['result'] is False and rec['clicks'] == [], str(rec['clicks']))

# 17.3 OCR 坐标框现在带宽高（供上面的"取最宽"启发式使用）
with mock.patch.object(uo_module, "__file__", os.path.join(off_dir, "Underlying_Operations.py")):
    probe_boxes = ops.analyse_pic_word_with_boxes("boxes_probe")
check("OCR 坐标框带 w/h 字段",
      bool(probe_boxes) and all('w' in b and 'h' in b and b['w'] > 0 and b['h'] > 0
                                for b in probe_boxes), str(probe_boxes))
analyser.resolution_ratio_x, analyser.resolution_ratio_y = saved_ratio

# ---------- 18. 开奖倒计时识别：按文字形态找，不依赖位置 ----------
# 用例全部来自真机日志里实际出现过的 OCR 结果
for raw, expect_min, expect_sec in [
        ('13 : 47大容量立体多功育', 13, 47),
        ('12 : 11大容量立体多功育', 12, 11),
        ('13:47', 13, 47),
        ('03：42', 3, 42),
        ('倒计时 09 : 05 后开奖', 9, 5)]:
    boxes = [{'text': raw, 'x': 800, 'y': 1700, 'w': 200, 'h': 40, 'score': 0.9}]
    found = analyser.find_countdown_text(boxes)
    parsed = analyser.operation.check_countdown(found)
    check("倒计时形态识别：{!r} -> {}秒".format(raw, expect_min * 60 + expect_sec),
          bool(found) and parsed and parsed[0] == expect_min * 60 + expect_sec,
          "{!r} -> {!r}".format(found, parsed and parsed[0]))

# 真机日志里"读不到倒计时"的那些情况，不能被误判成倒计时
for raw in ['丝塔美自修次红部缘护一变盛库', '炸专场可以直接', '货正品保障',
            '发送评论：右上角领券，1+2号合并付！到手318元']:
    found = analyser.find_countdown_text(
        [{'text': raw, 'x': 500, 'y': 2200, 'w': 800, 'h': 60, 'score': 0.9}])
    check("倒计时形态识别：商品描述不会被误判 -> {!r}".format(raw[:12]), found == '', repr(found))

# 倒计时在面板里的位置变了也能找到（这正是旧实现失效的场景）
moved_boxes = [{'text': '9.9参考价值', 'x': 400, 'y': 1650, 'w': 200, 'h': 40, 'score': 0.9},
               {'text': '11 : 26', 'x': 900, 'y': 1780, 'w': 150, 'h': 40, 'score': 0.9},
               {'text': '任务', 'x': 200, 'y': 2210, 'w': 100, 'h': 40, 'score': 0.9}]
found = analyser.find_countdown_text(moved_boxes)
check("倒计时位置变化后仍能找到（旧固定裁剪会漏）",
      found == '11 : 26' and analyser.operation.check_countdown(found)[0] == 686,
      "{!r}".format(found))

# 多个候选时取最短的那行（真机数据："14 : 39" vs 粘着商品描述的 "10 : 36大容量立体多功育"）
mixed_boxes = [{'text': '10 : 36大容量立体多功育', 'x': 900, 'y': 1700, 'w': 400, 'h': 40,
                'score': 0.9},
               {'text': '14 : 39', 'x': 900, 'y': 1780, 'w': 150, 'h': 40, 'score': 0.9}]
mixed_found = analyser.find_countdown_text(mixed_boxes)
check("倒计时形态识别：多候选取最短的那行（避免粘上商品描述）",
      mixed_found == '14 : 39' and analyser.operation.check_countdown(mixed_found)[0] == 879,
      "{!r}".format(mixed_found))

# get_fudai_contain_and_countdown：内容会排除任务/按钮/倒计时那几行
saved_18 = (analyser.read_fudai_panel, analyser.cut_pic)
analyser.read_fudai_panel = lambda top_y, mode=0: [
    {'text': '鱼竿套装', 'x': 500, 'y': 1700, 'w': 300, 'h': 50, 'score': 0.9},
    {'text': '08 : 30', 'x': 900, 'y': 1780, 'w': 150, 'h': 40, 'score': 0.9},
    {'text': '任务', 'x': 200, 'y': 2210, 'w': 100, 'h': 40, 'score': 0.9},
    {'text': '一键发表评论', 'x': 640, 'y': 2525, 'w': 290, 'h': 70, 'score': 0.9}]
content_text, countdown_text = analyser.get_fudai_contain_and_countdown(1345)
check("面板识别：倒计时取到 08:30", countdown_text == '08 : 30', repr(countdown_text))
check("面板识别：内容只保留奖品行，排除任务与按钮",
      content_text == '鱼竿套装', repr(content_text))
check("面板识别：内容不含任务/按钮字样",
      '任务' not in content_text and '评论' not in content_text, repr(content_text))
analyser.read_fudai_panel, analyser.cut_pic = saved_18

# 面板顶部坐标异常时依然安全返回
check("面板识别：top_y 异常时不报错",
      analyser.get_fudai_contain_and_countdown(None) == ('', ''))

# ---------- 19. 抽奖统计：中奖与未中都要计数 ----------
init_stats = analyser.status.snapshot()['stats']
check("统计项：初始就包含中奖与未中",
      'win' in init_stats and 'lose' in init_stats, str(init_stats))

# add_stat 直接验
tally_status = FudaiStatus()
tally_status.add_stat('win')
tally_status.add_stat('lose')
tally_status.add_stat('lose', 2)
tally_snapshot = tally_status.snapshot()['stats']
check("统计：add_stat 正确累加", (tally_snapshot['win'], tally_snapshot['lose']) == (1, 3),
      str(tally_snapshot))
check("统计：add_stat 返回更新后的统计", tally_status.add_stat('win')['win'] == 2,
      str(tally_status.snapshot()['stats']))

# 悬浮窗文本行
empty_lines = format_snapshot_lines(FudaiStatus().snapshot())
check("文本行：显示中奖/未中/合计",
      any('中奖0  未中0  合计0' in line for line in empty_lines), str(empty_lines))
tally_status2 = FudaiStatus()
tally_status2.add_stat('win')
tally_status2.add_stat('lose')
tally_status2.add_stat('lose')
tally_lines = [line for line in format_snapshot_lines(tally_status2.snapshot())
               if line.startswith('抽奖统计')]
check("文本行：中奖1 未中2 合计3",
      tally_lines and tally_lines[0] == '抽奖统计：中奖1  未中2  合计3', str(tally_lines))
check("文本行：队列统计全为0时不显示（不占位置）",
      not any(line.startswith('队列统计')
              for line in format_snapshot_lines(tally_status2.snapshot())))
tally_status2.add_stat('missed')
check("文本行：队列有数据时才显示队列统计",
      any(line.startswith('队列统计')
          for line in format_snapshot_lines(tally_status2.snapshot())))


# 主循环联动：真的走完"参与 → 等开奖 → 处理结果"，看统计有没有涨
def draw_delta(result_value):
    saved_check = analyser.check_lucky_draw_result
    saved_reward = analyser.get_reward
    analyer_check = analyser.check_lucky_draw_result
    analyser.check_lucky_draw_result = lambda: result_value
    analyser.get_reward = lambda *a, **k: None
    before = analyser.status.snapshot()['stats']
    try:
        drive_main_loop(have_fudai=True, time_text='0342', attend_result=True, max_rounds=1)
    finally:
        analyser.check_lucky_draw_result = saved_check
        analyser.get_reward = saved_reward
    after = analyser.status.snapshot()['stats']
    return after['win'] - before['win'], after['lose'] - before['lose']


delta_lose = draw_delta(1)                 # 1 = 没有抽中
check("主循环：没抽中时未中计数 +1（旧版本完全不计数）",
      delta_lose == (0, 1), str(delta_lose))
delta_win = draw_delta(1438)               # 返回 y 坐标 = 中奖
check("主循环：中奖时中奖计数 +1", delta_win == (1, 0), str(delta_win))
delta_coupon = draw_delta(2)               # 2 = 没中但给了优惠券，也算未中
check("主循环：没中但得优惠券也算未中", delta_coupon == (0, 1), str(delta_coupon))
delta_unknown = draw_delta(3)              # 3 = 交互按钮未知，不计数以免统计失真
check("主循环：结果未知时不计数（避免统计失真）", delta_unknown == (0, 0), str(delta_unknown))

# 统计能同时反映中奖与未中（累计展示）
final_stats = analyser.status.snapshot()['stats']
check("累计统计同时包含中奖与未中",
      final_stats['win'] >= 1 and final_stats['lose'] >= 2, str(final_stats))

# ---------- 20. 开奖结果判定：宽区域 + 关键词（修复"永远判定不出结果"）----------
def result_boxes(*lines, height=60):
    """把 (文本, 高度) 变成 check_lucky_draw_result 需要的坐标框"""
    return [{'text': text, 'x': 540, 'y': 1300 + index * 100, 'w': 400,
             'h': (h if h is not None else height), 'score': 0.9}
            for index, (text, h) in enumerate(lines)]


def judge_result(boxes):
    saved_read = analyser.read_result_bands
    analyser.read_result_bands = lambda *a, **k: list(boxes)
    try:
        return analyser.check_lucky_draw_result()
    finally:
        analyser.read_result_bands = saved_read


check("开奖判定：没有抽中 + 我知道了 -> 1",
      judge_result(result_boxes(('没有抽中', 60), ('我知道了', 40))) == 1)
check("开奖判定：没有抽中 + 领取并使用 -> 2",
      judge_result(result_boxes(('没有抽中', 60), ('领取并使用', 40))) == 2)
check("开奖判定：只有标题、按钮没认出 -> 3（不猜）",
      judge_result(result_boxes(('没有抽中', 60))) == 3)
check("开奖判定：抽中福袋 -> 返回协议行的基准 y",
      judge_result(result_boxes(('抽中福袋', 60), ('已阅读并同意用户协议', 40))) == 1300 + 100)
check("开奖判定：中奖但认不到协议行 -> 用兜底位置",
      judge_result(result_boxes(('抽中福袋', 60))) == analyser.RESULT_DEFAULT_AGREEMENT_Y)
check("开奖判定：没有弹窗 -> False",
      judge_result(result_boxes(('说点什么...', 30), ('直播间商品', 30))) is False)
check("开奖判定：空区域 -> False", judge_result([]) is False)

# 直播间滚动评论里出现"没中奖"这种小字，不能被当成本局结果
check("开奖判定：小字评论里的'没中奖'不误判",
      judge_result(result_boxes(('没中奖', 22))) is False
      and judge_result(result_boxes(('没中奖', 22), ('我知道了', 40))) is False)
check("开奖判定：大标题'未中奖'能认出来",
      judge_result(result_boxes(('未中奖', 60), ('我知道了', 40))) == 1)

# 坐标换算：两条带各自的 y 偏移都要加上（用 1280x2772 才能验出换算，基准分辨率下偏移恰好等于带起点）
saved_20 = (analyser.cut_pic, ops.analyse_pic_word_with_boxes, ops.get_screenshot,
            analyser.resolution_ratio_x, analyser.resolution_ratio_y)
analyser.resolution_ratio_x, analyser.resolution_ratio_y = 1280, 2772
analyser.cut_pic = lambda *a, **k: True
ops.get_screenshot = lambda *a, **k: True
ops.analyse_pic_word_with_boxes = lambda name, mode=0: (
    [{'text': '带0', 'x': 10, 'y': 50, 'w': 100, 'h': 40, 'score': 0.9}] if name == 'draw_result0'
    else [{'text': '带1', 'x': 10, 'y': 50, 'w': 100, 'h': 40, 'score': 0.9}])
band_boxes = analyser.read_result_bands()
expect_band0 = 200 * 2772 // 2400          # = 231
expect_band1 = 1050 * 2772 // 2400         # = 1212
check("开奖判定：两条带的 y 偏移分别换算正确",
      [b['y'] for b in band_boxes] == [50 + expect_band0, 50 + expect_band1],
      str([b['y'] for b in band_boxes]))
(analyser.cut_pic, ops.analyse_pic_word_with_boxes, ops.get_screenshot,
 analyser.resolution_ratio_x, analyser.resolution_ratio_y) = saved_20

# 已参与过的面板：带里只有任务说明，不能点它，要认成"已参与等开奖"
rec = run_attend('加入购物粉丝团已达成发送评论：右上角领券，1+2号合并付！到手318元已达成请勿离开直播间，否则会失去抽奖资格哦',
                 [{'text': '发送评论：右上角领券，1+2号合并付！到手318元', 'x': 515, 'y': 2373,
                   'w': 867, 'h': 60, 'score': 0.9}])
check("已参与面板：不点任务说明长句（真机日志回归）",
      (515, 2373) not in rec['clicks'], str(rec['clicks']))
check("已参与面板：识别为已参与、等待开奖", rec['result'] is True, str(rec['result']))

# 关键词被拆到两个框里（只有拼接文本才含关键字）时，退回固定位置盲点一次
rec = run_attend('参与抽奖', [{'text': '参与', 'x': 100, 'y': 2500, 'w': 60, 'h': 40, 'score': 0.9},
                          {'text': '抽奖', 'x': 200, 'y': 2500, 'w': 60, 'h': 40, 'score': 0.9}])
check("关键词被拆框、找不到整行按钮时：退回固定位置盲点一次",
      rec['result'] is True and any(c == (500, 2060) for c in rec['clicks']), str(rec['clicks']))

# ---------- 21. 设备偏移值改存运行时缓存（不再污染 config.json）----------
import json as _json  # noqa: E402
import shutil as _shutil  # noqa: E402

cache_path = os.path.join(REPO_ROOT, ops.RUNTIME_CACHE_PATH)
config_path = os.path.join(REPO_ROOT, 'config.json')
cache_backup = None
if os.path.exists(cache_path):
    cache_backup = open(cache_path, 'rb').read()

config_before = open(config_path, 'rb').read()
offset_before = ops.load_device_offsets()
check("偏移值：能读到 config.json 里的历史记录（兼容旧配置）",
      offset_before.get('XXXXXXX') == -50, str(offset_before))

try:
    wrote = ops.update_config_with_offsets(device_offsets={'smoke-test-device': 12})
    check("偏移值：新设备写入运行时缓存", wrote is True)
    check("偏移值：缓存文件已生成且内容正确",
          os.path.exists(cache_path)
          and _json.load(open(cache_path, encoding='utf-8'))['device_offsets'].get('smoke-test-device') == 12,
          open(cache_path, encoding='utf-8').read()[:120] if os.path.exists(cache_path) else '缺失')
    check("偏移值：写完不再改动被版本管理的 config.json",
          open(config_path, 'rb').read() == config_before)
    check("偏移值：老记录与新记录能同时读出",
          ops.load_device_offsets().get('XXXXXXX') == -50
          and ops.load_device_offsets().get('smoke-test-device') == 12,
          str(ops.load_device_offsets()))
    check("偏移值：重复写入同一设备返回 False（不覆盖已有记录）",
          ops.update_config_with_offsets(device_offsets={'smoke-test-device': 99}) is False)
    check("偏移值：旧式调用签名也能兼容",
          ops.update_config_with_offsets('config.json', {'smoke-test-device2': 7}) is True
          and ops.load_device_offsets().get('smoke-test-device2') == 7)
    check("偏移值：device_cache.json 已被 .gitignore 忽略",
          sp.run(['git', 'check-ignore', ops.RUNTIME_CACHE_PATH],
                 cwd=REPO_ROOT, capture_output=True).returncode == 0)
finally:
    if cache_backup is None:
        if os.path.exists(cache_path):
            os.remove(cache_path)
    else:
        open(cache_path, 'wb').write(cache_backup)
    open(config_path, 'wb').write(config_before)
check("偏移值：测试后已恢复原状",
      open(config_path, 'rb').read() == config_before
      and ops.load_device_offsets().get('smoke-test-device') is None)

# ---------- 22. 开奖后的等待：不再瞎等（旧实现硬编码 180~420 秒）----------
saved_22 = (ops.delay, analyser.quick_check_have_fudai,
            analyser.after_draw_wait_min, analyser.after_draw_wait_max,
            analyser.after_draw_wait_check)


def run_pause(waited_delays, min_s, max_s, check_s, finds_at=None, rounds=1):
    """驱动 pause_after_draw：finds_at 指定第几次探路时"发现福袋" """
    record = {'delays': [], 'checks': 0}

    def fake_delay(seconds=1):
        record['delays'].append(seconds)

    def fake_quick_check():
        record['checks'] += 1
        return finds_at is not None and record['checks'] >= finds_at

    ops.delay = fake_delay
    analyser.quick_check_have_fudai = fake_quick_check
    analyser.after_draw_wait_min = min_s
    analyser.after_draw_wait_max = max_s
    analyser.after_draw_wait_check = check_s
    try:
        actual = analyser.pause_after_draw()
    finally:
        (ops.delay, analyser.quick_check_have_fudai, analyser.after_draw_wait_min,
         analyser.after_draw_wait_max, analyser.after_draw_wait_check) = saved_22
    return actual, record


actual, rec = run_pause(None, 60, 60, 30)
check("开奖后等待：按片等待，总时长不超上限",
      actual == 60 and rec['delays'] == [30, 30], "{} {}".format(actual, rec['delays']))
check("开奖后等待：每片结束都探路一次", rec['checks'] == 2, str(rec['checks']))

actual, rec = run_pause(None, 60, 60, 30, finds_at=1)
check("开奖后等待：发现福袋就提前结束（不等满）",
      actual == 30 and rec['delays'] == [30] and rec['checks'] == 1,
      "{} {} {}".format(actual, rec['delays'], rec['checks']))

actual, rec = run_pause(None, 0, 0, 30)
check("开奖后等待：配置为 0 时完全不等待",
      actual == 0 and rec['delays'] == [] and rec['checks'] == 0,
      "{} {}".format(actual, rec['delays']))

actual, rec = run_pause(None, 90, 90, 100)
check("开奖后等待：探路间隔大于总时长时只等一片",
      actual == 90 and rec['delays'] == [90] and rec['checks'] == 1,
      "{} {} {}".format(actual, rec['delays'], rec['checks']))

check("开奖后等待：默认值已从硬编码的180~420改短",
      (analyser.DEFAULT_AFTER_DRAW_WAIT_MIN, analyser.DEFAULT_AFTER_DRAW_WAIT_MAX) == (30, 90),
      "{}/{}".format(analyser.DEFAULT_AFTER_DRAW_WAIT_MIN, analyser.DEFAULT_AFTER_DRAW_WAIT_MAX))
check("开奖后等待：配置项已生效",
      (analyser.after_draw_wait_min, analyser.after_draw_wait_max,
       analyser.after_draw_wait_check) == (30, 90, 30),
      "{}/{}/{}".format(analyser.after_draw_wait_min, analyser.after_draw_wait_max,
                        analyser.after_draw_wait_check))
def has_hardcoded_draw_wait(path):
    """AST 级检查：只认真正的 random_delay(180, 420) 调用，注释里的字样不算"""
    tree = ast.parse(open(path, encoding='utf-8').read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'random_delay'):
            continue
        values = [arg.value for arg in node.args if isinstance(arg, ast.Constant)]
        if values == [180, 420]:
            return True
    return False


check("开奖后等待：主循环里已不再硬编码 random_delay(180, 420)",
      not has_hardcoded_draw_wait(os.path.join(REPO_ROOT, 'douyin_fudai.py')))

# 探路函数本身：找到福袋时要更新"上次找到福袋时间"，避免被 30 分钟兜底误判
saved_22b = (ops.get_screenshot, analyser.find_fudai_icon, analyser.last_find_fudai_time)
ops.get_screenshot = lambda *a, **k: True
analyser.find_fudai_icon = lambda arr: (60, 466)
analyser.last_find_fudai_time = 0.0
with mock.patch.object(df_module, '__file__', os.path.join(off_dir, 'douyin_fudai.py')), \
        mock.patch.object(uo_module, '__file__', os.path.join(off_dir, 'Underlying_Operations.py')), \
        mock.patch('PIL.Image.open', lambda *a, **k: Image.new('RGB', (1280, 2772), (10, 10, 10))):
    quick_found = analyser.quick_check_have_fudai()
check("探路：发现福袋时返回 True 并更新时间戳",
      quick_found is True and analyser.fudai_hit == (60, 466)
      and analyser.last_find_fudai_time > 0, str(analyser.fudai_hit))
(ops.get_screenshot, analyser.find_fudai_icon, analyser.last_find_fudai_time) = saved_22b

# ---------- 汇总 ----------
failed = [r for r in results if not r[1]]
print("\n===== 冒烟结果: %d/%d 通过 =====" % (len(results) - len(failed), len(results)))
for name, _, detail in failed:
    print("  失败:", name, detail)
sys.exit(1 if failed else 0)
