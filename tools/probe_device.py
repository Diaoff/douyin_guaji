"""真机自检 / 标定工具（多房间排队抢福袋的前置步骤）

为什么要这个工具
----------------
"快到开奖时回到对应直播间"依赖两件我无法离线确定的事：
1. 直播间顶部的**房名区域**到底在哪、OCR 能不能稳定认出来
2. 关注列表里每张卡片的**名称在什么位置**、点哪里才能真正进房

所以坐标目前是猜的（ROOM_TITLE_CROP / FOLLOW_LIST_FULL_CROP），
本工具就是用来在真机上把这两件事量出来。另外它顺带做分辨率自检，
能立刻发现"wm size 与实际截图尺寸不一致"这种会导致整套坐标错位的问题。

用法（在项目根目录执行，手机已连接并打开抖音）
------------------------------------------------
    .venv\\Scripts\\python.exe tools\\probe_device.py
        分辨率自检 + 打印当前屏幕顶部区域的实际 OCR 结果（在直播间里跑）

    .venv\\Scripts\\python.exe tools\\probe_device.py --dump-list
        先手动进入「抖音 → 我 → 关注 → 直播中」列表，再跑这条；
        打印整屏识别出的每段文字及其坐标，用于标定卡片点击位置

    .venv\\Scripts\\python.exe tools\\probe_device.py --find-room 关键词
        同样先在关注列表页，试跑一次"按名找房并点进去"
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from PIL import Image  # noqa: E402
from douyin_fudai import fudai_analyse  # noqa: E402

SCREENSHOT_PATH = os.path.join(REPO_ROOT, 'pic', 'screenshot.png')


def report_resolution(analyser):
    """分辨率自检：wm size 的 Physical/Override 与实际截图尺寸是否一致"""
    ok, output = analyser.operation._adb(['shell', 'wm', 'size'], device_id=analyser.device_id)
    physical = override = None
    for line in (output or '').splitlines():
        line = line.strip()
        if line.startswith('Physical size:'):
            physical = line.split(':', 1)[1].strip()
        elif line.startswith('Override size:'):
            override = line.split(':', 1)[1].strip()

    print("=== 分辨率自检 ===")
    print("wm size : Physical={} Override={}".format(physical, override))
    if override and override != physical:
        print("  ⚠ 检测到 Override：程序按 Physical 算坐标，实际渲染用 Override，坐标会整体错位")
        print("    修法：把 Override 也纳入取值顺序，或直接用截图尺寸当基准")

    width, height = analyser.operation.get_device_resolution(analyser.device_id)
    analyser.resolution_ratio_x, analyser.resolution_ratio_y = width, height
    analyser.operation.get_screenshot(analyser.device_id)
    if not os.path.exists(SCREENSHOT_PATH):
        print("  ✗ 没拿到截图，后续检查跳过")
        return None

    with Image.open(SCREENSHOT_PATH) as img:
        shot_w, shot_h = img.size
    kx, ky = shot_w / 1080, shot_h / 2400
    print("截图    : {}x{}（与 wm size {}x{} {}）".format(
        shot_w, shot_h, width, height, "一致" if (shot_w, shot_h) == (width, height) else "不一致"))
    if (shot_w, shot_h) != (width, height):
        print("  ⚠ 不一致：应以截图尺寸为准，截图才是真正渲染出来的画面")
    print("缩放    : x={:.4f} y={:.4f} {}".format(
        kx, ky, "等比" if abs(kx - ky) < 1e-9 else "**非等比**（长宽比不是9:20，y轴会累积偏差）"))
    print("关键坐标推算（用来看顶部元素偏差有多大）：")
    for name, base_y in (('福袋图标', 403), ('底部评论框', 2166), ('底部导航"我"', 2200)):
        print("  基准 y={:<5} -> 本机 y={:.0f}".format(base_y, base_y * ky))
    return shot_w, shot_h


def dump_top_area(analyser):
    """打印直播间顶部区域的实际 OCR 结果，用于标定房名区域"""
    analyser.operation.get_screenshot(analyser.device_id)
    print("=== 当前屏幕顶部区域 OCR（标定房名用）===")
    for label, crop in (('默认房名区域 {}'.format(analyser.ROOM_TITLE_CROP), analyser.ROOM_TITLE_CROP),
                        ('更宽的顶部条 (0,80,1080,260)', (0, 80, 1080, 260))):
        analyser.cut_pic(*crop, '', 'probe_title')
        # 整块裁剪的 OCR 坐标要加上裁剪起点才是屏幕坐标
        boxes = analyser.operation.analyse_pic_word_with_boxes('probe_title')
        print("-- {} --".format(label))
        if not boxes:
            print("   （没识别到文字）")
        for item in boxes:
            screen_x = item['x'] + int(crop[0] * analyser.resolution_ratio_x // 1080)
            screen_y = item['y'] + int(crop[1] * analyser.resolution_ratio_y // 2400)
            print("   {!r:30s} 屏幕坐标=({}, {}) 置信度={:.2f}".format(
                item['text'], screen_x, screen_y, item['score']))
        print("   归一化 key = {!r}".format(
            analyser.room_key_from_title(''.join(i['text'] for i in boxes))))


def dump_follow_list(analyser):
    """打印关注列表整屏 OCR 结果（标定卡片点击位置用）"""
    analyser.operation.get_screenshot(analyser.device_id)
    analyser.cut_pic(*analyser.FOLLOW_LIST_FULL_CROP, '', 'follow_list_full')
    boxes = analyser.operation.analyse_pic_word_with_boxes('follow_list_full')
    print("=== 关注列表整屏 OCR（标定卡片位置用）===")
    print("整屏裁剪下坐标即设备像素，可直接用于点击")
    for item in sorted(boxes, key=lambda b: b['y']):
        print("   y={:<5} x={:<5} {!r:28s} key={!r}".format(
            item['y'], item['x'], item['text'], analyser.room_key_from_title(item['text'])))
    print("共 {} 段文字".format(len(boxes)))
    if not boxes:
        print("   （没识别到文字：确认手机当前就在「关注 → 直播中」列表页）")


def main():
    parser = argparse.ArgumentParser(description='真机自检 / 多房间标定工具')
    parser.add_argument('--dump-list', action='store_true',
                        help='打印关注列表整屏 OCR 结果（需先手动进入关注→直播中列表）')
    parser.add_argument('--find-room', metavar='KEY',
                        help='按关键词在当前关注列表里找房并点进去（需先进入列表页）')
    args = parser.parse_args()

    analyser = fudai_analyse()
    device_id = analyser.operation.select_device()
    if not device_id:
        print("没找到设备，先确认 adb devices 能看到手机")
        return 1
    analyser.device_id = device_id

    report_resolution(analyser)

    if args.find_room:
        print("=== 按名找房测试：{} ===".format(args.find_room))
        ok = analyser.find_room_in_follow_list(analyser.room_key_from_title(args.find_room))
        print("结果：{}".format("已进入目标直播间" if ok else "没找到 / 没进去"))
    elif args.dump_list:
        dump_follow_list(analyser)
    else:
        dump_top_area(analyser)
        print()
        print("提示：把上面房名区域的实际结果和我猜的 ROOM_TITLE_CROP={} 对照，".format(
            analyser.ROOM_TITLE_CROP))
        print("      如果认不到房名，就把那个区域调整到能覆盖房名的位置。")
        print("      接着用 --dump-list 标定关注列表的卡片坐标。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
