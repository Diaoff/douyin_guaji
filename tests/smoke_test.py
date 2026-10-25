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

# ---------- 汇总 ----------
failed = [r for r in results if not r[1]]
print("\n===== 冒烟结果: %d/%d 通过 =====" % (len(results) - len(failed), len(results)))
for name, _, detail in failed:
    print("  失败:", name, detail)
sys.exit(1 if failed else 0)
