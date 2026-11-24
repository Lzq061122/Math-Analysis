"""错题照片的磁盘存储。

照片统一存在数据目录下的 uploads/ 里，文件名由程序生成，不接受外部路径。
网页版与桌面版共用这一份实现：桌面版把照片复制进来，老师在网页版也能看到。
"""
import os


def _fault(name):
    """测试用故障注入；老师正常使用时 MISTAKE_FAULT 不存在，不会触发。"""
    if name in (os.environ.get("MISTAKE_FAULT") or "").split(","):
        raise OSError("模拟故障：%s" % name)


class UploadStore:
    """上传图片的磁盘存储；文件名由服务端生成，不接受客户端路径。"""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def path(self, name):
        safe = os.path.basename(name)
        return os.path.join(self.root, safe)

    def save(self, name, blob):
        _fault("photo_save")
        with open(self.path(name), "wb") as fh:
            fh.write(blob)

    def remove(self, names):
        removed = 0
        for name in names:
            try:
                os.remove(self.path(name))
                removed += 1
            except FileNotFoundError:
                pass
        return removed
