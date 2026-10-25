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
        stdout = b''
        for key, value in outputs.items():
            if key in joined:
                stdout = value.encode()
                break
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
                    min_seconds=15, max_minutes=15, needswitch=False, max_rounds=6):
    """驱动真实的 fudai_choujiang 主循环，返回观察到的事件。

    have_fudai=False 时每轮都判定不到福袋（验证换房间计数）；
    have_fudai=True 时每轮都找到福袋，并按 time_text 走倒计时判定。
    三处跳出点：轮次上限 / 换房间(swipe) / 参与抽奖(attend_choujiang)。
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
        raise _LoopEscape()

    patch(ops, 'delay', fake_delay)
    patch(ops, 'get_screenshot', lambda *a, **k: True)
    patch(ops, 'get_current_hour', lambda: 12)      # 固定白天，避开夜间停机
    patch(ops, 'click', lambda *a, **k: None)
    patch(ops, 'get_device_resolution', lambda *a, **k: (1080, 2400))
    patch(analyser, 'find_y_offset', lambda *a, **k: 0)
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

# ---------- 汇总 ----------
failed = [r for r in results if not r[1]]
print("\n===== 冒烟结果: %d/%d 通过 =====" % (len(results) - len(failed), len(results)))
for name, _, detail in failed:
    print("  失败:", name, detail)
sys.exit(1 if failed else 0)
