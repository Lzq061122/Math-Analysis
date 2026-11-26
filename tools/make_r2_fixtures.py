"""R2-01 夹具的确定性生成器：重建 `tests/fixtures/` 下的两个 ZIP。

    C:\\Python314\\python.exe tools\\make_r2_fixtures.py            # 重建并打印 sha256
    C:\\Python314\\python.exe tools\\make_r2_fixtures.py --check    # 只校验（不写文件）

产物：
  tests/fixtures/R2_ZIP模板.zip     老师用的标准模板（answers.csv + images/ 三张示例图）
  tests/fixtures/R2_验收_10x5.zip   合成验收包：10 名学生 × 5 道题 = 50 条

为什么要一个生成器（而不是只提交二进制）：
1. 二进制进了 git 就没法 diff，改错了没人看得出来——「包内容被悄悄换掉」会让 R2-06
   全链路验收里 50/45/15 这些预期值全部失效，且塌得没法解释；
2. 生成过程**确定性**：条目时间戳固定、内容固定、不带随机量，重建必须逐字节一致，
   所以 `--check` 能当门禁用（不一致就说明有人改了包或改了代码）；
3. 派单 §9.1 要求「可重复导入或重置的合成数据」，脚本本身就是那个「可重复」的证据。

验收包的内容按 R2-05/R2-02 的错因口径铺开（去括号 / 移项 / 合并同类项 / 系数化为 1），
让 R2-06 串「导入 → 核验 → 错因与成绩 → 看板 → PDF」时不必再换一套数据：
  第 1 题 去括号（`bracket_rule`）     第 2 题 移项（`sign_error` / `term_move_rule`）
  第 3 题 合并同类项（`like_terms_rule`）  第 4 题 系数化为 1        第 5 题 正确作答
样例分布：S01 第 1 题两张图；S02 第 2 题一张图；S03 第 3 题**仅照片**（内容不足）；
S04 第 4 题**声明了但包里没有**的图（缺图提示）；S10 五题全部缺测（作答人数 = 9）。
"""

import csv
import hashlib
import io
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from app import answer_import_zip as Z   # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
TEMPLATE_PATH = os.path.join(FIXTURES, "R2_ZIP模板.zip")
PACKAGE_PATH = os.path.join(FIXTURES, "R2_验收_10x5.zip")

HEADER = ["记录编号", "学生姓名", "学校编号", "题号", "题目内容", "单元", "标准解法",
          "学生步骤", "来源批次", "作答日期", "缺测", "缺测原因", "图片路径"]
UNIT = "一元一次方程"

# 每题：题干 / 标准解 / 该题要覆盖的错因（写进注释，供 R2-06 对口径）
QUESTIONS = {
    "1": {
        "text": "解方程 3(x-2)=12",
        "standard": "3x-6=12；3x=18；x=6",
        "error": "3x-2=12；3x=14；x=14/3",        # 括号外漏乘
        "cause": "bracket_rule",
        "correct": "3x-6=12；3x=18；x=6",
    },
    "2": {
        "text": "解方程 5x-3=2x+6",
        "standard": "5x-2x=6+3；3x=9；x=3",
        "error": "5x+2x=6+3；7x=9；x=9/7",           # 移项不变号
        "cause": "term_move_rule",
        "correct": "5x-2x=6+3；3x=9；x=3",
    },
    "3": {
        "text": "解方程 4x+3x=21",
        "standard": "7x=21；x=3",
        "error": "7x=21；x=21/7=2",                  # 合并后系数化错
        "cause": "like_terms_rule",
        "correct": "7x=21；x=3",
    },
    "4": {
        "text": "解方程 2x=8",
        "standard": "x=4",
        "error": "x=8",                              # 忘记两边同除
        "cause": "coefficient_rule",
        "correct": "x=4",
    },
    "5": {
        "text": "解方程 x+3=7",
        "standard": "x=4",
        "error": "",
        "cause": "",
        "correct": "x=4",
    },
}


def answer_rows():
    rows = []
    for index in range(1, 11):
        code = "S%02d" % index
        name = "学生%02d" % index
        for question, spec in QUESTIONS.items():
            record_key = "A-%s-%s" % (code, question)
            steps = spec["error"] or spec["correct"]
            missing = "否"
            reason = ""
            images = ""
            if index == 10:
                steps = ""
                missing = "是"
                reason = "请假未测"
            elif index == 1 and question == "1":
                images = "images/%s-%s-1.png;images/%s-%s-2.png" % (code, question, code, question)
            elif index == 2 and question == "2":
                images = "images/%s-%s-1.png" % (code, question)
            elif index == 3 and question == "3":
                steps = ""                                    # 仅照片 → 内容不足
                images = "images/%s-%s-1.png" % (code, question)
            elif index == 4 and question == "4":
                images = "images/%s-%s-缺图.png" % (code, question)   # 声明了但包里没有
            rows.append([record_key, name, code, question, spec["text"], UNIT, spec["standard"],
                         steps, "", "2026-09-10", missing, reason, images])
    return rows


def csv_text(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(HEADER)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def package_zip():
    rows = answer_rows()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        Z._write_entry(archive, "answers.csv", csv_text(rows).encode("utf-8-sig"))
        for name in ("images/S01-1-1.png", "images/S01-1-2.png",
                     "images/S02-2-1.png", "images/S03-3-1.png"):
            Z._write_entry(archive, name, Z._ONE_PIXEL_PNG)
    return buffer.getvalue()


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def main(argv):
    check_only = "--check" in argv
    template = Z.template_zip()
    package = package_zip()
    # 确定性自证：同一进程重建两次必须逐字节相同
    assert template == Z.template_zip(), "模板重建不一致（条目时间戳没固定？）"
    assert package == package_zip(), "验收包重建不一致（条目时间戳没固定？）"

    targets = ((TEMPLATE_PATH, template), (PACKAGE_PATH, package))
    problems = []
    for path, blob in targets:
        if check_only:
            current = open(path, "rb").read() if os.path.exists(path) else b""
            same = current == blob
            print("%s  %s  %s" % ("OK  " if same else "DIFF", os.path.basename(path), sha(blob)))
            if not same:
                problems.append(path)
        else:
            with open(path, "wb") as fh:
                fh.write(blob)
            print("写入 %s  sha256=%s  %d 字节" % (os.path.basename(path), sha(blob), len(blob)))
    if problems:
        print("以下夹具体与代码不一致，请删掉后重新生成：%s" % "、".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
