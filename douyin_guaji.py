from douyin_fudai import fudai_analyse

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

if __name__ == '__main__':
    choujiang = fudai_guaji()
    choujiang.guaji()
