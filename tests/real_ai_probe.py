"""真实 AI 服务的连通性探针（可选，不参与软件功能）。

用途：在老师本机已有可用服务时，用一张自制数学题图发一次真实的
OpenAI 兼容图片请求，核对地址、鉴权、图片输入、结果解析和用量字段。

配置只从本机读取，不写进命令行参数，也不打印密钥：
  · 环境变量：AI_TEST_BASE、AI_TEST_MODEL、AI_TEST_KEY
  · 或文件：tests/real_ai.env，每行 KEY=VALUE（同上三个键）
base 填服务商给的基础地址（例如 https://api.example.com/v1），
程序会自动补 /chat/completions。

运行：
  .venv\\Scripts\\python.exe tests\\real_ai_probe.py

真实调用会产生费用或消耗额度，是否执行由老师决定。
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from app import vision  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ENV_FILE = ROOT / "tests" / "real_ai.env"
QUESTION = "解方程：3(x-2)=2x+5"
SOLUTION = "3x-6=2x+5\n3x-2x=5+6\nx=11"


def load_settings():
    values = {
        "base": os.environ.get("AI_TEST_BASE", ""),
        "model": os.environ.get("AI_TEST_MODEL", ""),
        "key": os.environ.get("AI_TEST_KEY", ""),
    }
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip().upper()
            if name in ("AI_TEST_BASE", "BASE"):
                values["base"] = value.strip()
            elif name in ("AI_TEST_MODEL", "MODEL"):
                values["model"] = value.strip()
            elif name in ("AI_TEST_KEY", "KEY"):
                values["key"] = value.strip()
    return values


def make_image(path):
    image = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 80), QUESTION, fill="black")
    for index, line in enumerate(SOLUTION.splitlines()):
        draw.text((60, 180 + index * 60), line, fill="black")
    image.save(str(path), "PNG")
    return path


def usage_line(usage):
    if not isinstance(usage, dict) or not any(
            usage.get(k) is not None for k in
            ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "billing_source")):
        return "用量：尚未确认（服务没有返回用量、费用和计费来源）"
    parts = [
        "prompt %s / completion %s / total %s" % tuple(
            "尚未确认" if usage.get(k) is None else str(usage.get(k))
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")),
    ]
    cost = usage.get("cost")
    parts.append("费用：%s" % ("尚未确认" if cost is None else "$%s" % cost))
    source = usage.get("billing_source")
    parts.append("计费来源：%s" % ("尚未确认" if source in (None, "") else source))
    return "；".join(parts)


def main():
    settings = load_settings()
    missing = [name for name, key in
               (("AI_TEST_BASE", "base"), ("AI_TEST_MODEL", "model"), ("AI_TEST_KEY", "key"))
               if not settings[key]]
    if missing:
        print("缺少真实服务配置：%s" % "、".join(missing))
        print("请在本机设置环境变量，或新建 tests\\real_ai.env 填写；不要把密钥发到聊天里。")
        return 2

    config = {
        "mode": vision.MODE_CUSTOM,
        "service_name": "真实服务探针",
        "base_url": settings["base"],
        "model": settings["model"],
        "api_key": settings["key"],
        "allow_env": False,
    }
    resolved = vision.resolve_config(config)
    if not resolved.get("address_ok") or not resolved.get("key_configured"):
        print("配置检查未通过：%s" % (resolved.get("address_error") or resolved.get("reason") or ""))
        return 2

    image_path = ROOT / "docs" / "screenshots" / "real-probe-自制题图.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    make_image(image_path)
    job = {
        "image_bytes": image_path.read_bytes(),
        "image_mime": "image/png",
        "question_text": QUESTION,
        "solution_text": SOLUTION,
        "trial": True,
    }
    print("服务：%s" % resolved.get("service"))
    print("模型：%s" % resolved.get("model"))
    print("请求地址：%s" % resolved.get("request_url"))
    print("密钥：已配置（只在本机内存，不打印）")
    started = time.time()
    try:
        result = vision.CloudVisionProvider(resolved).analyze(job)
    except vision.AnalysisError as exc:
        print("失败（%s）：%s" % (exc.code, exc))
        return 2
    elapsed = int((time.time() - started) * 1000)
    print("成功；耗时 %d 毫秒" % elapsed)
    print("知识点：%s" % "、".join(result.get("knowledge_points") or []))
    print("错误类型：%s" % (result.get("error_type") or "尚未确认"))
    print(usage_line(result.get("usage")))
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "service": resolved.get("service"),
        "model": resolved.get("model"),
        "request_url": resolved.get("request_url"),
        "elapsed_ms": elapsed,
        "knowledge_points": result.get("knowledge_points"),
        "error_type": result.get("error_type"),
        "usage": result.get("usage"),
    }
    out = ROOT / "docs" / "verify-ai-146-real.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print("原始结果：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
