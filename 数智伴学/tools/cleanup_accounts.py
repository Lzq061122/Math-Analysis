"""按用户名精确清理测试账号及其班级、学生、错题与照片。

只删除 --username 明确列出的账号；不按模式批量删除，也不动运行前已有的账号。
"""

import argparse
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.path.join(BASE_DIR, "data"))
    parser.add_argument("--username", action="append", required=True)
    args = parser.parse_args()

    db_path = os.path.join(args.data_dir, "app.db")
    uploads_dir = os.path.join(args.data_dir, "uploads")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    removed_users = []
    removed_files = 0
    for name in dict.fromkeys(args.username):
        row = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()
        if not row:
            print("跳过（不存在）：" + name)
            continue
        files = [
            r["image_file"]
            for r in conn.execute(
                "SELECT m.image_file FROM mistakes m JOIN classes c ON c.id=m.class_id"
                " WHERE c.user_id=? AND m.image_file<>''",
                (row["id"],),
            )
        ]
        conn.execute("DELETE FROM users WHERE id=?", (row["id"],))
        for filename in files:
            path = os.path.join(uploads_dir, os.path.basename(filename))
            if os.path.exists(path):
                os.remove(path)
                removed_files += 1
        removed_users.append(name)

    conn.commit()
    print("已删除账号：%s" % (", ".join(removed_users) if removed_users else "无"))
    print("已删除照片：%d 个" % removed_files)
    conn.close()


if __name__ == "__main__":
    main()
