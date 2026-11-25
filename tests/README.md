# 测试环境与运行方式

本目录的脚本用于复现 1.8.1 的测试结果。全部使用合成数据与临时目录，不读取日常数据目录，不调用真实云端服务。

## 环境

- Windows 10/11，Python 3.11（交付测试使用 3.11.15）
- 依赖：PySide6 6.6.3.1、Pillow、pywinauto、pywin32
- 只有重新打包 EXE 时才需要 PyInstaller 6.22.3

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install "PySide6==6.6.3.1" Pillow pywinauto pywin32
```

## 运行

在源码根目录执行，日志写到 `docs/`，临时库写到 `tests/.tmp-*`。

| 分组 | 命令 | 日志 |
| --- | --- | --- |
| 作答导入（源码） | `.venv\Scripts\python.exe tests\verify_answers_150.py` | `docs/verify-answers-150.txt` |
| 旧功能回归 | `.venv\Scripts\python.exe tests\verify_core_150.py` | `docs/verify-core-150.txt` |
| 导入界面 | `set QT_QPA_PLATFORM=offscreen` 后 `.venv\Scripts\python.exe tests\verify_answers_ui.py` | `docs/verify-answers-ui.txt` |
| 导入 EXE | `.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_answers_exe.py` | `docs/verify-answers-exe.txt` |
| 自定义 AI（源码） | `.venv\Scripts\python.exe tests\verify_ai_146.py` | `docs/verify-ai-146.txt` |
| AI 界面 | `set QT_QPA_PLATFORM=offscreen` 后 `.venv\Scripts\python.exe tests\verify_ai_146_ui.py` | `docs/verify-ai-146-ui.txt` |
| AI EXE | `.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_ai_146_exe.py` | `docs/verify-ai-exe.txt` |
| 作答分析（源码） | `.venv\Scripts\python.exe tests\verify_review_160.py` | `docs/verify-review-160.txt` |
| 作答分析界面 | `set QT_QPA_PLATFORM=offscreen` 后 `.venv\Scripts\python.exe tests\verify_review_ui.py` | `docs/verify-review-ui.txt` |
| 作答分析 EXE | `.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_review_exe.py` | `docs/verify-review-exe.txt` |
| 个人学情（源码） | `.venv\Scripts\python.exe tests\verify_tutor_170.py` | `docs/verify-tutor-170.txt` |
| 个人学情界面 | `set QT_QPA_PLATFORM=offscreen` 后 `.venv\Scripts\python.exe tests\verify_tutor_ui.py` | `docs/verify-tutor-ui.txt` |
| 教师短追问（源码） | `.venv\Scripts\python.exe tests\verify_followup_180.py` | `docs/verify-followup-180.txt` |
| 追问界面 | `set QT_QPA_PLATFORM=offscreen` 后 `.venv\Scripts\python.exe tests\verify_followup_ui.py` | `docs/verify-followup-ui.txt` |
| 追问 EXE（两种窗口尺寸的截图） | `.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_followup_exe.py` | `docs/verify-followup-exe.txt` |
| 小屏与缩放（EXE） | `.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_layout_exe.py` | `docs/verify-layout-exe.txt` |

五个 EXE 测试要求存在 `build\程序包\数智伴学-1.8.1.zip`（可用交付的程序包放入该路径），并会打印 ZIP 与 EXE 的 SHA-256。`run_hidden.py` 在独立桌面启动窗口，不会把窗口弹到当前桌面。

`verify_layout_exe.py` 按目标逻辑区域模拟三种屏幕：1366×768、1920×1080、1366×768@125%（逻辑 1093×614）。本机面板是 2560×1600 物理像素、系统缩放 150%，屏幕装不下 1366×768 逻辑区域，所以用 `QT_SCALE_FACTOR` 把 Qt 缩放比调到与目标情形一致，再按目标区域乘缩放的物理像素摆放窗口；缩放比例与物理尺寸会写进日志头部。

AI 相关测试使用 `tests/ai_stub_server.py` 本机模拟服务，不发送任何内容到外部。测试脚本通过 `POST /__usage` 切换用量形态、`POST /__fail` 注入失败（`ok`、`400`、`length`、`maxtokens`、`slow`、`garbage`）、`POST /__review` 切换作答分析的模型返回、`POST /__tutor` 切换辅导建议的模型返回（`ok`/`fabricate`/`uncertain`/`empty`）、`POST /__followup` 切换追问候选与错因候选的返回（同上四种）。

## fixtures/legacy146

存放 1.4.6 的 `db.py` 与 `backup.py` 快照，只用于两件事：构造旧结构数据库验证升级迁移，以及用旧版代码验证它对 format 4 备份的拒绝提示。没有学生数据，也不参与软件运行。

## fixtures/legacy160

一个由 1.6.0 程序写入的数据库（合成数据：教师 exe_teacher、班级「初一 2 班」、两条作答、一条已确认审核与一条已拒绝审核），用于验证 1.6.2 打开旧版数据后作答、审核关联、错题统计与备份恢复正常。

## 真实服务测试

`real_ai_probe.py` 只在你本机填写 `tests/real_ai.env` 或环境变量后才发起真实请求；仓库与源码包都不含该文件。
