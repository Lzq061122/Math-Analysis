"""演示数据：虚构的班级、学生、错题与配图。

- 姓名、题目、解题过程全部虚构，只用于演示界面流程。
- 首次启动自动创建演示账号 demo / demo1234 与一个演示班级。
- 演示错题的「原图」用 SVG 现场生成，不依赖第三方库。
"""

import html
from datetime import date, timedelta

from .analysis import MockProvider
from .auth import create_user
from .db import now_iso
from .records import insert_mistake

DEMO_USER = "demo"
DEMO_PASSWORD = "demo1234"

STUDENTS = [
    ("林一诺", "一组"),
    ("周子航", "三组"),
    ("周子航", "五组"),
    ("陈思远", ""),
    ("吴嘉禾", ""),
    ("郑雨萌", ""),
]


def _svg_lowres(width, height):
    """低分辨率演示图：只放两行提示，避免正文在小图里被裁切。"""
    size = max(11, int(width / 12))
    small = max(9, int(size * 0.8))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
        '<rect width="100%%" height="100%%" fill="#f3f4f6"/>'
        '<text x="50%%" y="44%%" text-anchor="middle" font-family="Microsoft YaHei, sans-serif"'
        ' font-size="%d" fill="#6b7280">演示：低分辨率照片</text>'
        '<text x="50%%" y="58%%" text-anchor="middle" font-family="Microsoft YaHei, sans-serif"'
        ' font-size="%d" fill="#9ca3af">仅用于演示“信息不足”提示</text>'
        "</svg>"
    ) % (width, height, width, height, size, small)
    return svg.encode("utf-8")


def _svg(question, solution, width, height, tag):
    if min(width, height) < 320:
        return _svg_lowres(width, height)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (width, height, width, height),
        "<style>"
        "text{font-family:'Microsoft YaHei','PingFang SC',sans-serif;font-size:20px;fill:#1f2937}"
        ".q{font-size:22px;font-weight:600}.a{fill:#1d4ed8}.note{fill:#9ca3af;font-size:15px}"
        "</style>",
        '<rect width="100%" height="100%" fill="#fffdf7"/>',
        '<rect x="24" y="24" width="%d" height="%d" fill="none" stroke="#e5e7eb"/>' % (width - 48, height - 48),
        '<text x="48" y="64" class="note">演示配图 · 虚构数据 · %s</text>' % html.escape(tag),
    ]
    y = 120
    for line in (question or "（演示：无题目文本）").split("\n"):
        parts.append('<text x="48" y="%d" class="q">%s</text>' % (y, html.escape(line)))
        y += 34
    y += 24
    parts.append('<text x="48" y="%d" class="note">学生解答</text>' % y)
    y += 38
    for line in (solution or "（演示：无解答文本）").split("\n"):
        parts.append(
            '<text x="48" y="%d" class="a" transform="rotate(-1.2 48 %d)">%s</text>'
            % (y, y, html.escape(line))
        )
        y += 40
    parts.append("</svg>")
    return "".join(parts).encode("utf-8")


DEMO_MISTAKES = [
    {
        "student": 0,
        "source_type": "作业",
        "source_detail": "课本 P42 第 3 题",
        "days_ago": 1,
        "question": "解方程：3(x - 2) = 2x + 5",
        "solution": "3x - 6 = 2x + 5\n3x + 2x = 5 + 6\n5x = 11\nx = 11/5",
        "status": "confirmed",
    },
    {
        "student": 1,
        "source_type": "考试",
        "source_detail": "单元测第 18 题",
        "days_ago": 2,
        "question": "计算：1/(x-1) + 1/(x+1)",
        "solution": "= 1/(x-1) + 1/(x+1)\n= 2/((x-1)+(x+1))\n= 2/(2x)\n= 1/x",
        "status": "confirmed",
    },
    {
        "student": 2,
        "source_type": "作业",
        "source_detail": "练习册 2.3",
        "days_ago": 2,
        "question": "解方程：2x + 7 = 3x - 5",
        "solution": "2x + 7 = 3x - 5\n2x + 3x = -5 - 7\n5x = -12\nx = -12/5",
        "status": "confirmed",
    },
    {
        "student": 3,
        "source_type": "考试",
        "source_detail": "月考第 22 题",
        "days_ago": 4,
        "question": "已知二次函数 y = x² - 4x + 3，求它的顶点坐标。",
        "solution": "y = (x² - 4x + 4) + 3\n  = (x-2)² + 3\n顶点 (2, 3)",
        "status": "confirmed",
    },
    {
        "student": 4,
        "source_type": "练习",
        "source_detail": "课堂练 5",
        "days_ago": 5,
        "question": "如图，AB∥CD，∠1 = 65°，求 ∠2 的度数。",
        "solution": "∠2 = 65°",
        "status": "confirmed",
    },
    {
        "student": 5,
        "source_type": "作业",
        "source_detail": "课本 P66 第 8 题",
        "days_ago": 6,
        "question": "一个不透明袋中有 3 个红球和 2 个白球，随机摸出一个球，摸到红球的概率是多少？",
        "solution": "摸了 10 次，7 次是红球，所以概率是 7/10",
        "status": "confirmed",
    },
    {
        "student": 0,
        "source_type": "作业",
        "source_detail": "练习册 3.1",
        "days_ago": 0,
        "question": "解不等式：-2x + 3 > 7",
        "solution": "-2x > 4\nx > -2",
        "status": "pending",
    },
    {
        "student": 0,
        "source_type": "作业",
        "source_detail": "课本 P48 第 7 题",
        "days_ago": 8,
        "question": "计算：2/(x+2) - 1/(x-2)",
        "solution": "= (2-1)/((x+2)-(x-2))\n= 1/4",
        "status": "confirmed",
    },
    {
        "student": 1,
        "source_type": "考试",
        "source_detail": "周测第 12 题",
        "days_ago": 9,
        "question": "解方程：4x - 3 = 2(x + 1) + 5",
        "solution": "4x - 3 = 2x + 2 + 5\n4x + 2x = 7 + 3\n6x = 10\nx = 5/3",
        "status": "confirmed",
    },
    {
        "student": 1,
        "source_type": "练习",
        "source_detail": "课堂练 3",
        "days_ago": 11,
        "question": "如图，∠1 = ∠2，说明 AB∥CD 的理由。",
        "solution": "因为 ∠1 = ∠2\n所以 AB∥CD",
        "status": "confirmed",
    },
    {
        "student": 3,
        "source_type": "考试",
        "source_detail": "单元测第 20 题",
        "days_ago": 1,
        "question": "证明：两直线平行，内错角相等。",
        "solution": "因为 ∠1 = ∠2，所以 AB∥CD，\n所以内错角相等。",
        "status": "pending",
    },
    {
        "student": 4,
        "source_type": "作业",
        "source_detail": "课本 P51 第 6 题",
        "days_ago": 3,
        "question": "化简：(x² + 4)/(x + 2)",
        "solution": "= (x² + 4)/(x + 2)\n= x + 2",
        "status": "returned",
        "returned_reason": "照片模糊，解题过程看不清，需重拍",
    },
    {
        "student": 5,
        "source_type": "作业",
        "source_detail": "",
        "days_ago": 0,
        "question": "",
        "solution": "",
        "status": "pending",
    },
    {
        "student": 1,
        "source_type": "考试",
        "source_detail": "周测第 9 题",
        "days_ago": 0,
        "question": "解方程：5(x+1) = 3x - 7",
        "solution": "5x + 5 = 3x - 7\n5x + 3x = -7 - 5\n8x = -12\nx = -3/2",
        "status": "pending",
        "low_quality": True,
    },
]


def ensure_seed(db, uploads):
    """首次启动写入演示账号与数据；已有演示账号则跳过。"""
    if db.scalar("SELECT COUNT(*) FROM users WHERE username=?", (DEMO_USER,)):
        return False
    user_id = create_user(db, DEMO_USER, DEMO_PASSWORD, "演示老师")
    today = date.today()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO classes(user_id, name, grade, note, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (user_id, "初二(3)班", "八年级", "演示班级，数据全部虚构", now_iso(), now_iso()),
        )
        class_id = cur.lastrowid
        student_ids = []
        for name, note in STUDENTS:
            cur = conn.execute(
                "INSERT INTO students(class_id, name, note, created_at, updated_at) VALUES (?,?,?,?,?)",
                (class_id, name, note, now_iso(), now_iso()),
            )
            student_ids.append(cur.lastrowid)

    # 演示数据固定用模拟分析生成，避免配置真实密钥后启动时自动调用外部服务。
    provider = MockProvider()
    for index, item in enumerate(DEMO_MISTAKES, start=1):
        width, height = (240, 180) if item.get("low_quality") else (900, 1200)
        image_name = "demo-%d-%d.svg" % (class_id, index)
        uploads.save(image_name, _svg(item["question"], item["solution"], width, height, item["source_type"]))
        result = provider.analyze(
            {
                "question_text": item["question"],
                "solution_text": item["solution"],
                "image_file": image_name,
                "image_width": width,
                "image_height": height,
                "blurry": bool(item.get("blurry")),
            }
        )
        occurred = (today - timedelta(days=item.get("days_ago", 0))).isoformat()
        mistake_id = insert_mistake(
            db,
            class_id,
            student_ids[item["student"]],
            image_name,
            "image/svg+xml",
            width,
            height,
            {
                "source_type": item["source_type"],
                "source_detail": item["source_detail"],
                "note": "",
                "occurred_on": occurred,
            },
            result,
        )
        if item["status"] == "confirmed":
            with db.tx() as conn:
                conn.execute(
                    "UPDATE mistakes SET status='confirmed', confirmed_at=?, confirmed_by=? WHERE id=?",
                    (now_iso(), user_id, mistake_id),
                )
        elif item["status"] == "returned":
            with db.tx() as conn:
                conn.execute(
                    "UPDATE mistakes SET status='returned', returned_at=?, returned_reason=? WHERE id=?",
                    (now_iso(), item.get("returned_reason", ""), mistake_id),
                )
    return True
