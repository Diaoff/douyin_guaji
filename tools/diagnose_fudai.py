"""福袋识别诊断器：为什么没识别到福袋？

逐个回答这四个问题（全部基于真机截图，不靠猜）：
1. 分辨率对不对：wm size 报的尺寸、截图实际尺寸、x/y 缩放比是否一致
2. 程序在哪些像素点上找福袋、那些点现在是什么颜色
3. 目标颜色（福袋图标颜色）在整张截图里到底有没有、在哪
4. 要让程序找到它，y 偏移值需要是多少；这个值在页面的自动搜索范围(±50)里吗

用法（在项目根目录，手机停在有福袋的直播间）：
    .venv\\Scripts\\python.exe tools\\diagnose_fudai.py

可选参数：
    --image 路径   不重新截图，直接分析已有图片
    --serial 序列号 指定设备（默认取 adb devices 里第一台）
"""

import argparse
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

# ---- 与 douyin_fudai.check_have_fudai 保持一致的判定参数 ----
BASE_W, BASE_H = 1080, 2400
TARGET_RGB = {'r': (183, 193), 'g': (172, 193), 'b': (237, 248)}
SLOTS = [(33, 52), (159, 178), (285, 304), (411, 430)]   # 4 个横向槽位
BASE_BAG_Y = 403                                          # 基准福袋行
OFFSET_RANGE = range(-50, 51, 2)                          # find_y_offset 的搜索范围


def adb(serial, *args, timeout=60):
    cmd = ['adb'] + (['-s', serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def pick_serial(requested=None):
    if requested:
        return requested
    out = adb(None, 'devices').stdout or ''
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            return parts[0]
    return None


def grab_screenshot(serial, local_path):
    ok = adb(serial, 'shell', 'screencap', '-p', '/sdcard/DCIM/diag.png')
    if ok.returncode != 0:
        print("截图失败:", (ok.stderr or ok.stdout).strip()[:200])
        return False
    pulled = adb(serial, 'pull', '/sdcard/DCIM/diag.png', local_path)
    if pulled.returncode != 0:
        print("拉取截图失败:", (pulled.stderr or pulled.stdout).strip()[:200])
        return False
    adb(serial, 'shell', 'rm', '/sdcard/DCIM/diag.png')
    return True


def in_target(px):
    """px 形如 (r, g, b)，判断是否落在 check_have_fudai 的目标颜色范围"""
    return (TARGET_RGB['r'][0] <= px[0] <= TARGET_RGB['r'][1]
            and TARGET_RGB['g'][0] <= px[1] <= TARGET_RGB['g'][1]
            and TARGET_RGB['b'][0] <= px[2] <= TARGET_RGB['b'][1])


def main():
    parser = argparse.ArgumentParser(description='福袋识别诊断器')
    parser.add_argument('--image', help='直接分析已有截图，不重新截屏')
    parser.add_argument('--serial', help='指定设备序列号')
    args = parser.parse_args()

    print("=" * 62)
    print("福袋识别诊断")
    print("=" * 62)

    serial = None
    if not args.image:
        serial = pick_serial(args.serial)
        if not serial:
            print("没找到可用设备（adb devices 里没有 state=device 的设备）")
            return 1

    # ---------- 1) 分辨率 ----------
    print("\n【1】分辨率")
    if serial:
        raw = (adb(serial, 'shell', 'wm', 'size').stdout or '').strip()
        for line in raw.splitlines():
            print("   wm size:", line.strip())
        physical = re.search(r'Physical size:\s*(\d+)x(\d+)', raw)
        override = re.search(r'Override size:\s*(\d+)x(\d+)', raw)
        if override and physical and override.group(0) != physical.group(0):
            print("   ⚠ 存在 Override，程序当前取的是 Physical —— 若两者不同，坐标会整体错位")

    image_path = args.image or os.path.join(REPO_ROOT, 'pic', 'diagnose_screenshot.png')
    if not args.image:
        print("   正在截图...")
        if not grab_screenshot(serial, image_path):
            return 1

    img = Image.open(image_path)
    shot_w, shot_h = img.size
    kx, ky = shot_w / BASE_W, shot_h / BASE_H
    print("   截图实际尺寸: {}x{}".format(shot_w, shot_h))
    print("   缩放比: x={:.4f}  y={:.4f}  ->  {}".format(
        kx, ky, "等比" if abs(kx - ky) < 1e-9 else "**非等比**（长宽比与基准 1080x2400 不同）"))
    print("   基准 y=403(福袋行) 会被算成 y={}".format(int(BASE_BAG_Y * ky)))
    print("   基准 y=2166(底部评论框，用于标定偏移) 会被算成 y={}".format(int(2166 * ky)))
    if abs(kx - ky) > 1e-9:
        print("   ⚠ x/y 缩放不一致：横轴和纵轴按不同比例换算，坐标必然有系统性偏差")

    arr = np.asarray(img.convert('RGB')).astype(np.int16)
    height, width = arr.shape[:2]

    # ---------- 2) 程序实际探测的点是什么颜色 ----------
    print("\n【2】程序实际探测的像素点（y_pianyi=0 时）")
    probe_y = int(BASE_BAG_Y * ky)
    for slot_index, (x0, x1) in enumerate(SLOTS):
        xs = [int(x * kx) for x in range(x0, x1)]
        xs = [x for x in xs if 0 <= x < width]
        if not xs:
            continue
        colors = [tuple(int(v) for v in arr[min(probe_y, height - 1), x]) for x in xs]
        hit = [c for c in colors if in_target(c)]
        print("   槽位{} x={}..{} (实际 {}-{}) y={} : 命中{}个  样例颜色 {}".format(
            slot_index + 1, x0, x1, xs[0], xs[-1], probe_y, len(hit),
            colors[:3]))

    # ---------- 3) 目标颜色在整张截图里有没有 ----------
    print("\n【3】目标颜色（福袋图标紫）在整张截图里的分布")
    mask = ((arr[:, :, 0] >= TARGET_RGB['r'][0]) & (arr[:, :, 0] <= TARGET_RGB['r'][1]) &
            (arr[:, :, 1] >= TARGET_RGB['g'][0]) & (arr[:, :, 1] <= TARGET_RGB['g'][1]) &
            (arr[:, :, 2] >= TARGET_RGB['b'][0]) & (arr[:, :, 2] <= TARGET_RGB['b'][1]))
    total = int(mask.sum())
    print("   命中像素: {} 个（占 {:.4f}%）".format(total, total / (height * width) * 100))
    if total:
        rows = np.nonzero(mask.any(axis=1))[0]
        cols = np.nonzero(mask.any(axis=0))[0]
        print("   分布范围: y {}-{}  x {}-{}".format(rows[0], rows[-1], cols[0], cols[-1]))
        # 按行聚类，报告主要色块
        row_hits = mask.sum(axis=1)
        bands = []
        current = None
        for y in range(height):
            if row_hits[y] > 0:
                if current is None:
                    current = [y, y]
                else:
                    current[1] = y
            elif current is not None:
                bands.append(tuple(current))
                current = None
        if current is not None:
            bands.append(tuple(current))
        bands.sort(key=lambda b: -(b[1] - b[0]))
        print("   主要色块（按高度排序，最多5个）:")
        for y0, y1 in bands[:5]:
            sub = mask[y0:y1 + 1]
            xs = np.nonzero(sub.any(axis=0))[0]
            print("      y {}-{} (高{})  x {}-{}  像素{}个  基准坐标 y≈{}".format(
                y0, y1, y1 - y0 + 1, xs[0], xs[-1], int(sub.sum()),
                int(y0 / ky) if ky else 0))
    else:
        print("   ⚠ 整张截图里没有任何像素落在目标颜色范围内")
        print("     可能原因：这个福袋不是普通款(可能是超级/钻石款，颜色不同)，")
        print("               或者当前页面根本不是直播间")

    # ---------- 3b) 放宽范围找候选 ----------
    print("\n【3b】放宽范围找“偏紫”像素（用于判断是不是颜色范围太窄）")
    loose = ((arr[:, :, 2] > 200) & (arr[:, :, 2] - arr[:, :, 1] > 40)
             & (arr[:, :, 0] > arr[:, :, 1]) & (arr[:, :, 0] > 120))
    loose_total = int(loose.sum())
    print("   偏紫像素: {} 个".format(loose_total))
    if loose_total:
        row_hits = loose.sum(axis=1)
        top_rows = np.argsort(row_hits)[::-1][:6]
        print("   像素最多的几行（可能是图标所在行）:")
        for y in sorted(int(v) for v in top_rows if row_hits[v] > 20):
            xs = np.nonzero(loose[y])[0]
            print("      y={:<5} 该行{}个   x {}-{}   基准y≈{}  需要的y偏移={:+d}".format(
                y, int(row_hits[y]), xs[0], xs[-1], int(y / ky),
                int(y / ky) - BASE_BAG_Y))

    # ---------- 4) 自动搜索范围够不够 ----------
    print("\n【4】页面自动标定(±50)能否覆盖需要的偏移")
    found_offsets = []
    for offset in OFFSET_RANGE:
        y = int((BASE_BAG_Y + offset) * ky)
        if not 0 <= y < height:
            continue
        for (x0, x1) in SLOTS:
            for x in range(x0, x1):
                px_x = int(x * kx)
                if 0 <= px_x < width and in_target(arr[y, px_x]):
                    found_offsets.append(offset)
                    break
            else:
                continue
            break
    if found_offsets:
        print("   在这些 y 偏移下能命中: {}".format(found_offsets))
        print("   -> 页面标定范围(±50)可以覆盖，理论上 find_y_offset 能找到")
    else:
        print("   ✗ 在 ±50 的任何偏移下都命中不到目标颜色")
        print("     -> 说明不是偏移值的问题，需要改判定位或改颜色范围")

    # ---------- 5) 标注图 ----------
    if serial:
        marked = os.path.join(REPO_ROOT, 'pic', 'diagnose_marked.png')
        draw_img = img.convert('RGB').copy()
        draw = ImageDraw.Draw(draw_img)
        for (x0, x1) in SLOTS:
            draw.rectangle([int(x0 * kx), max(0, probe_y - 6), int(x1 * kx), probe_y + 6],
                           outline=(255, 0, 0), width=2)
        if total:
            rows = np.nonzero(mask.any(axis=1))[0]
            cols = np.nonzero(mask.any(axis=0))[0]
            draw.rectangle([int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])],
                           outline=(0, 255, 0), width=2)
        draw_img.save(marked)
        print("\n【5】已在截图上标注（红框=程序找的位置，绿框=目标颜色实际出现的位置）")
        print("   " + marked)
    return 0


if __name__ == '__main__':
    sys.exit(main())
