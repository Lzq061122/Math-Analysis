"""错题分析引擎。

默认使用 MockProvider；真实通道属于「独立组件」，正式教师业务流程不经过这里。

R1-07 起，本模块同时存放「个体数据不进生成式模型」这条业务边界的唯一文案与错误码：
- 业务侧（错题、作答、追问、辅导建议）在组装请求体之前就用它们拦断；
- 通道侧（vision.CloudVisionProvider）用它们拒绝任何非「离线工具用途」的发送。
边界依据：数智伴学_Agent开发规格.md §「业务环境只运行固定规则与人工流程，不发起个体化生成式请求」。

所有结果都带 mode 字段，界面据此区分真实与模拟，不冒充真实 AI。
"""

import hashlib

from .db import now_iso

MODE_MOCK = "mock"

# ---------------------------------------------------------------------------
# 个体数据边界：正式教师业务流程不向生成式模型发送可关联个人的内容
# ---------------------------------------------------------------------------
# 错误码固定为 ai_isolation，前端与测试据此区分「被隔离拦断」与「调用失败」。
INDIVIDUAL_FLOW_CODE = "ai_isolation"

INDIVIDUAL_FLOW_MESSAGE = (
    "正式教师业务流程不向生成式模型发送学生的作答、作答原文、身份或追问原话。"
    "本次没有组装请求，也没有把任何内容发出去；"
    "分析类内容请用规则定位与人工判断录入，结论照常可以保存与统计。"
)

# 唯一保留的非个体用途：离线工具/自检（AI 设置自检、本机桩验证、离线资源制作）。
# 该用途只允许发往本机回环地址，且不得携带任何学生记录；业务侧没有任何调用点。
PURPOSE_OFFLINE_TOOL = "offline_tool"
ALLOWED_EGRESS_PURPOSES = frozenset({PURPOSE_OFFLINE_TOOL})

# R1-14：教师账号显式开启「个体 AI」之后的用途标记。
# 它**不在** ALLOWED_EGRESS_PURPOSES 里 —— 只有 api 层确认「该账号已显式开启」并把授权
# 盖到 provider 上（`provider.individual_allowed = True`）之后，vision.egress_error 才认这个用途。
# 也就是说：默认关 = 这个用途根本不会出现；开启 = 只有被授权的那一个 provider 实例能发。
PURPOSE_TEACHER_INDIVIDUAL = "teacher_individual"

KNOWLEDGE_POINTS = [
    "一元一次方程",
    "一元二次方程",
    "分式运算",
    "整式与因式分解",
    "不等式",
    "函数与图象",
    "几何证明",
    "圆",
    "面积与周长",
    "统计与概率",
]

ERROR_TYPES = [
    "计算错误",
    "符号错误",
    "概念混淆",
    "审题遗漏",
    "步骤缺失",
    "表达不规范",
    "单位/格式",
    "其他",
]

CAUSES = [
    "知识理解不牢",
    "计算习惯",
    "审题不细",
    "解题策略不足",
    "表达与书写",
    "暂不确定",
]

# 关键词 → (知识点, 错误类型, 观察到的错误, 可能原因, 建议)
# 用于老师手动填写了题目/解题过程文本时的匹配，未命中则如实标记信息不足。
KEYWORD_RULES = [
    (("一元一次方程", "移项", "解方程"), "一元一次方程"),
    (("一元二次方程", "判别式", "配方", "求根公式"), "一元二次方程"),
    (("分式", "分母", "通分", "约分", "/("), "分式运算"),
    (("因式分解", "多项式", "单项式", "整式"), "整式与因式分解"),
    (("不等式", "解集"), "不等式"),
    (("函数", "图象", "坐标", "斜率"), "函数与图象"),
    (("证明", "全等", "相似", "内错角", "平行", "三角形", "∥", "∠"), "几何证明"),
    (("圆", "半径", "弦", "切线", "圆心角"), "圆"),
    (("面积", "周长", "体积"), "面积与周长"),
    (("概率", "频率", "平均数", "中位数", "众数", "方差"), "统计与概率"),
]

DETAIL_TEMPLATES = {
    "一元一次方程": {
        "error_type": "符号错误",
        "observed": [
            "解方程时把某一项从等号一侧移到另一侧，没有改变符号（常数项同样会出错）",
            "把 -2x 写成 2x 直接代入下一步，后续结果因此偏移",
        ],
        "causes": ["计算习惯", "符号规则掌握不牢"],
        "advice": "让学生在每一步移项后单独标出新符号，做 2 道同类移项题即时自查；课上用一道题板书对比“移项变号”和“同加同减”两种说法。",
    },
    "一元二次方程": {
        "error_type": "概念混淆",
        "observed": [
            "求根时对判别式 Δ 的符号判断有误，把 Δ<0 的情况当作有实数根处理",
            "配方过程中常数项补项后未在等式另一侧同步处理",
        ],
        "causes": ["知识理解不牢", "解题策略不足"],
        "advice": "先巩固判别式与根的关系，再练“先判 Δ、再求根”的固定流程；配方法用面积图辅助理解补项为什么要两侧同时加。",
    },
    "分式运算": {
        "error_type": "计算错误",
        "observed": [
            "把分子、分母分别合并，或直接把分子中的加号项当作因子约去，没有先通分或因式分解",
            "化简结果与原式不相等（可用 x 取特殊值代入检验）",
        ],
        "causes": ["知识理解不牢", "计算习惯"],
        "advice": "复习分式基本性质，强调“分子是多项式时先因式分解再约分”；用 3 道只差一个符号的对比题防止机械套用。",
    },
    "整式与因式分解": {
        "error_type": "步骤缺失",
        "observed": [
            "提公因式后括号内剩余项漏写常数项",
            "平方差公式中把减号写成加号",
        ],
        "causes": ["知识理解不牢", "表达与书写"],
        "advice": "让学生每完成一次因式分解后展开验算一步；把常用乘法公式做成小卡片随身对照。",
    },
    "不等式": {
        "error_type": "符号错误",
        "observed": [
            "两边同除以负数时没有改变不等号方向",
            "得到的解集代回原不等式不成立（如 x = 0 时原式不成立）",
        ],
        "causes": ["知识理解不牢", "计算习惯"],
        "advice": "把“乘除负数变号”作为单独训练点，连续做 5 题只改这一处；最后用代入检验法确认解集方向（取 x=-3、x=0 各试一次）。",
    },
    "函数与图象": {
        "error_type": "计算错误",
        "observed": [
            "配方时补上 +4 却没有在括号外减去 4，常数项仍写成原值",
            "由顶点式写顶点坐标时直接把常数项当作纵坐标",
        ],
        "causes": ["知识理解不牢", "计算习惯"],
        "advice": "配方练习固定写“加几减几”两行再化简；强调 y=(x-h)²+k 中顶点是 (h, k)，最后代回原式检验一次。",
    },
    "几何证明": {
        "error_type": "步骤缺失",
        "observed": [
            "只写结论或直接使用待证结论，没有标注每一步的依据（定理或已知条件）",
            "缺少必要的中间步骤，结论无法由已知推出",
        ],
        "causes": ["知识理解不牢", "表达与书写"],
        "advice": "要求每步证明后标注依据（定义/定理名称）；把平行线、全等、相似的判定条件做成对照表，比较缺一个条件时的反例。",
    },
    "圆": {
        "error_type": "概念混淆",
        "observed": [
            "把圆周角定理中的“同弧”条件忽略，直接用圆心角的一半",
            "切线与半径垂直的结论在未说明切线时被使用",
        ],
        "causes": ["知识理解不牢"],
        "advice": "用同一图形改变一个条件，让学生判断结论是否还成立；把定理的条件用彩色圈出，养成先看条件再用的习惯。",
    },
    "面积与周长": {
        "error_type": "单位/格式",
        "observed": [
            "面积单位写成 cm 而非 cm²",
            "组合图形分割后漏算其中一块",
        ],
        "causes": ["审题不细", "表达与书写"],
        "advice": "计算前先写单位、算后回看单位是否与所求一致；组合图形要求先画出分割线再逐块列式。",
    },
    "统计与概率": {
        "error_type": "概念混淆",
        "observed": [
            "把试验频率（如 7/10）当作理论概率直接下结论，未说明这是估计",
            "没有指出试验次数较少时频率会波动，不能直接当作概率",
        ],
        "causes": ["知识理解不牢", "审题不细"],
        "advice": "用不同试验次数的数据对比频率波动，理解频率估计概率的含义；区分“理论概率”和“试验频率”的写法。",
    },
}

# 老师完全没有填写题目/解题过程文本时使用的虚构样例（仅用于演示流程）。
SAMPLE_LIBRARY = [
    {
        "sample_id": "S1",
        "question_text": "解方程：3(x - 2) = 2x + 5",
        "solution_text": "3x - 6 = 2x + 5\n3x + 2x = 5 + 6\n5x = 11\nx = 11/5",
        "knowledge_points": ["一元一次方程"],
        "error_type": "符号错误",
        "observed_errors": [
            "移项时把 2x 移到等号左边，没有变成 -2x，写成了 3x + 2x",
            "由此得到的 x = 11/5 代回原方程不成立，正确解是 x = 11",
        ],
        "possible_causes": ["计算习惯", "知识理解不牢"],
        "advice_text": "单独练 5 道含括号的一元一次方程，要求移项那一步先写“变号后的项”再抄下来；每道题最后把解代回原方程检验。",
    },
    {
        "sample_id": "S2",
        "question_text": "计算：1/(x-1) + 1/(x+1)",
        "solution_text": "= (x+1) + (x-1) / (x²-1)\n= 2x/(x²-1)",
        "knowledge_points": ["分式运算"],
        "error_type": "表达不规范",
        "observed_errors": [
            "通分后分子未加括号，书写上无法体现整体相加",
            "结果未说明 x ≠ ±1 的取值限制",
        ],
        "possible_causes": ["表达与书写"],
        "advice_text": "要求分子是多项式时一律加括号再运算；分式题完成后补写“当 x≠±1 时”取值范围。",
    },
    {
        "sample_id": "S3",
        "question_text": "已知二次函数 y = x² - 4x + 3，求它的顶点坐标。",
        "solution_text": "y = (x² - 4x + 4) + 3\n  = (x-2)² + 3\n顶点 (2, 3)",
        "knowledge_points": ["函数与图象", "一元二次方程"],
        "error_type": "计算错误",
        "observed_errors": [
            "配方时补了 +4 却没有在括号外减去 4，常数项仍写成 3",
            "由顶点式写成顶点坐标时把纵坐标直接取常数项",
        ],
        "possible_causes": ["知识理解不牢", "计算习惯"],
        "advice_text": "配方练习固定写“加几减几”两行；强调 y=(x-h)²+k 中顶点是 (h, k) 而不是 (h, k+…) 。",
    },
    {
        "sample_id": "S4",
        "question_text": "如图，AB∥CD，∠1 = 65°，求 ∠2 的度数。",
        "solution_text": "因为 AB∥CD，所以 ∠1 = ∠2\n∠2 = 65°",
        "knowledge_points": ["几何证明"],
        "error_type": "步骤缺失",
        "observed_errors": [
            "直接写“∠1 = ∠2”，没有指明是哪两条直线被哪条直线所截形成的内错角",
            "未说明所用判定依据，推理链不完整",
        ],
        "possible_causes": ["表达与书写", "知识理解不牢"],
        "advice_text": "要求平行线角度题每步都写“∵…∴…”并注明内错角/同位角；先做 3 道只写依据不计算的填空。",
    },
    {
        "sample_id": "S5",
        "question_text": "一个不透明袋中有 3 个红球和 2 个白球，随机摸出一个球，摸到红球的概率是多少？",
        "solution_text": "摸了 10 次，7 次是红球，所以概率是 7/10",
        "knowledge_points": ["统计与概率"],
        "error_type": "概念混淆",
        "observed_errors": [
            "用试验频率 7/10 代替理论概率 3/5",
            "没有指出试验次数较少时频率会波动",
        ],
        "possible_causes": ["知识理解不牢"],
        "advice_text": "对比“理论概率 3/5”与“试验频率 7/10”，说明频率随试验次数增加趋近概率；补充等可能事件的计数方法。",
    },
    {
        "sample_id": "S6",
        "question_text": "解不等式：-2x + 3 > 7",
        "solution_text": "-2x > 4\nx > -2",
        "knowledge_points": ["不等式"],
        "error_type": "符号错误",
        "observed_errors": [
            "两边同除以 -2 时没有改变不等号方向，解集写成 x > -2（应为 x < -2）",
            "没有用代入检验确认解集方向",
        ],
        "possible_causes": ["知识理解不牢", "计算习惯"],
        "advice_text": "把“除以负数变号”单独列成一条检查项，做题后在草稿上打勾；用代入检验法验证解集（取 x=-3、x=0 各试一次）。",
    },
]

MIN_IMAGE_SIDE = 320
MIN_TEXT_LEN = 6


def _pick_sample(seed_bytes):
    digest = hashlib.sha256(seed_bytes or b"").hexdigest()
    return SAMPLE_LIBRARY[int(digest[:8], 16) % len(SAMPLE_LIBRARY)]


def _match_knowledge(text):
    """按关键词出现次数打分，取得分最高的知识点；同分时取排在前面的规则。"""
    best_point = ""
    best_score = 0
    for keywords, point in KEYWORD_RULES:
        score = sum(text.count(kw) for kw in keywords)
        if score > best_score:
            best_point = point
            best_score = score
    return best_point


def _insufficient(reason, question_text="", solution_text="", knowledge_points=None, advice=""):
    return {
        "mode": MODE_MOCK,
        "question_text": question_text,
        "solution_text": solution_text,
        "knowledge_points": knowledge_points or [],
        "error_type": "",
        "observed_errors": [],
        "possible_causes": [],
        "advice_text": advice
        or "信息不足：请重新拍摄清晰、完整的题目与解题过程照片，或在核对页手动填写题目与解题过程后重新分析。",
        "insufficient": True,
        "insufficient_reason": reason,
        "basis": "模拟分析模式：未接入图像识别，仅依据你填写的文字判断。",
        "analyzed_at": now_iso(),
    }


class AnalysisProvider:
    """分析服务接口。真实服务实现 analyze() 后由 get_provider 返回。"""

    mode = MODE_MOCK

    def analyze(self, job):
        raise NotImplementedError


class MockProvider(AnalysisProvider):
    """本地模拟分析：不联网、不读图片像素。"""

    mode = MODE_MOCK

    def analyze(self, job):
        question = (job.get("question_text") or "").strip()
        solution = (job.get("solution_text") or "").strip()
        width = int(job.get("image_width") or 0)
        height = int(job.get("image_height") or 0)
        blurry = bool(job.get("blurry"))

        if not job.get("image_file"):
            return _insufficient("没有上传图片，无法获得题目信息。", question, solution)

        if blurry:
            return _insufficient(
                "老师标注了图片不清晰或解题过程不完整，本次不给出具体错误结论。",
                question,
                solution,
                _match_knowledge(question + solution) and [ _match_knowledge(question + solution) ],
            )

        if width and height and min(width, height) < MIN_IMAGE_SIDE:
            return _insufficient(
                "图片分辨率过低（%d×%d），无法辨认题目与解题过程。" % (width, height),
                question,
                solution,
                _match_knowledge(question + solution) and [_match_knowledge(question + solution)],
            )

        if not question and not solution:
            sample = _pick_sample(job.get("image_file", "").encode("utf-8"))
            result = dict(sample)
            result.update(
                {
                    "mode": MODE_MOCK,
                    "knowledge_points": list(sample["knowledge_points"]),
                    "observed_errors": list(sample["observed_errors"]),
                    "possible_causes": list(sample["possible_causes"]),
                    "insufficient": False,
                    "insufficient_reason": "",
                    "basis": "模拟分析模式：未读取图片内容，本段分析取自内置的虚构示例，仅用于演示核对流程。",
                    "sample_id": sample["sample_id"],
                    "analyzed_at": now_iso(),
                }
            )
            return result

        text = question + "\n" + solution
        point = _match_knowledge(text)
        if not point:
            return _insufficient(
                "未能从你填写的文字中匹配到明确的数学知识点，请老师手动标注或补充题目信息。",
                question,
                solution,
            )

        template = DETAIL_TEMPLATES[point]
        if solution and len(solution) >= MIN_TEXT_LEN:
            return {
                "mode": MODE_MOCK,
                "question_text": question,
                "solution_text": solution,
                "knowledge_points": [point],
                "error_type": template["error_type"],
                "observed_errors": list(template["observed"]),
                "possible_causes": list(template["causes"]),
                "advice_text": template["advice"],
                "insufficient": False,
                "insufficient_reason": "",
                "basis": "模拟分析模式：根据你填写的文字匹配到「%s」的示例分析，未读取图片内容。" % point,
                "analyzed_at": now_iso(),
            }

        return _insufficient(
            "只提供了题目、没有提供学生的解题过程，无法定位具体错误位置。",
            question,
            solution,
            [point],
            "请补充学生的解题过程（手写步骤或文字），再重新分析以定位错误。",
        )


def get_provider(config=None):
    """按合并后的配置选择分析服务：配置完整时给出真实通道对象，否则用模拟分析。

    R1-07 起这里只是「构造对象」：真实通道的发送口（vision.CloudVisionProvider）只放行
    analysis.PURPOSE_OFFLINE_TOOL 且目标为本机回环的请求，业务侧的个体数据无法借它外发。
    环境变量默认不参与（resolve_config 的 allow_env 默认 False），旧变量也放不开。
    """
    from . import vision

    cfg = vision.resolve_config(config)
    if cfg["mode"] in vision.REMOTE_MODES:
        return vision.CloudVisionProvider(cfg)
    return MockProvider()
