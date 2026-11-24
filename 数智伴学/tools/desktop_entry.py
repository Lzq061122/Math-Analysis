"""PyInstaller 入口：打包后的 exe 从这里进入桌面界面。"""
import sys

from desktop.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
