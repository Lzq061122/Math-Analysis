"""测试日志的公共抬头：时间、命令、环境。

审查要求原始日志能对上「哪次运行、用什么命令、什么环境」，这里统一生成，
各脚本原样写进自己的日志文件。
"""

import datetime
import os
import platform
import sys


def environment_lines():
    return [
        "时间：%s" % datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "命令：%s" % " ".join([os.path.basename(sys.executable)] + sys.argv),
        "目录：%s" % os.getcwd(),
        "环境：Python %s / %s" % (platform.python_version(), platform.platform()),
    ]


def exit_line(code):
    return "退出码：%s" % code
