"""未采用的分析建议只放在当前进程的内存里，不落库。

每条建议绑定账号、登录会话、错题、分析开始时的记录指纹和任务标识；
采用、放弃、重新分析、退出登录、关闭窗口后即作废，失效标识不能再写库。
"""

import threading
import uuid


class PreviewError(Exception):
    """建议已失效或不属于当前账号、会话和错题。"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class PreviewStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def put(self, user_id, token, mistake_id, fingerprint, task_id, result):
        preview_id = uuid.uuid4().hex
        record = {
            "preview_id": preview_id,
            "user_id": int(user_id),
            "token": token or "",
            "mistake_id": int(mistake_id),
            "fingerprint": fingerprint,
            "task_id": task_id or "",
            "result": result,
        }
        with self._lock:
            # 同一账号同一条错题只保留最新一份建议，旧的直接作废。
            for key, item in list(self._items.items()):
                if item["user_id"] == record["user_id"] and item["mistake_id"] == record["mistake_id"]:
                    self._items.pop(key, None)
            self._items[preview_id] = record
        return record

    def take(self, preview_id, user_id, token, mistake_id):
        """取出并作废一条建议；账号、会话、错题任何一项不一致都拒绝。"""
        with self._lock:
            item = self._items.get(preview_id or "")
            if not item:
                raise PreviewError("这条建议已经失效，请重新分析")
            if item["user_id"] != int(user_id) or item["token"] != (token or ""):
                raise PreviewError("这条建议不属于当前登录的账号，请重新分析")
            if item["mistake_id"] != int(mistake_id):
                raise PreviewError("这条建议不是当前错题的，请重新分析")
            self._items.pop(preview_id, None)
            return item

    def drop(self, preview_id=None, user_id=None, token=None, mistake_id=None):
        with self._lock:
            for key, item in list(self._items.items()):
                if preview_id and key != preview_id:
                    continue
                if user_id is not None and item["user_id"] != int(user_id):
                    continue
                if token is not None and item["token"] != (token or ""):
                    continue
                if mistake_id is not None and item["mistake_id"] != int(mistake_id):
                    continue
                self._items.pop(key, None)

    def clear(self):
        with self._lock:
            self._items.clear()
