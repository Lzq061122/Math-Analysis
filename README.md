# 数智伴学 · 桌面版 1.8.1

给数学老师使用的 Windows 桌面软件：录入班级、学生、错题与照片，按班级批量导入作答 CSV，对一条作答做 AI 步骤分析（带原始步骤证据，老师确认后才生成错题），在「个人学情」里按近期权重看一个学生的优先问题、生成或改写辅导建议，再针对一个问题生成简短追问、记录学生回答，并让 AI 依据回答给出待核对的错因候选。账号、班级、学生、错题、照片、建议与追问记录都保存在本机数据目录。

## 目录

```
src/
  app/                 数据、错题、作答导入、作答分析、个人学情与辅导建议、教师短追问、照片、AI 请求与备份
  desktop/             PySide6 原生桌面界面（统一主题 theme.py）
  web/                 便携版网页界面与静态资源
  server.py            HTTP 服务入口（便携版/网页端）
  launcher.py          便携版启动器
tests/                 1.8.1 测试脚本（源码、界面、最终 EXE、本地模拟服务）
tools/                 构建、打包与维护脚本
docs/
  reports/             历史工作报告
  screenshots/         界面截图
  logs/                最近一次测试的原始日志
  源码说明.txt
  便携版使用说明.txt
  作答导入模板.csv
packaging/desktop/     随程序包分发的老师使用说明
licenses-desktop/      第三方许可
交付/1.8.1/            程序包、源码包、报告、测试日志与校验值
data/                  本机运行数据（不随包分发，不要删除）
```

## 源码运行

需要 Python 3.11、PySide6：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install pyside6 pillow
set PYTHONPATH=src
.venv\Scripts\python.exe -m desktop
```

网页/便携版：`python src\launcher.py`。

## 数据库升级

1.4.6 的数据目录可以直接打开：启动时先补 `students.code` 列、`answers` 表、`answer_reviews` 表、`tutor_advice` 表与 `followups`/`followup_updates` 表，补之前自动备份为 `app.db.bak-<时间戳>-<随机>`，失败则不动原库并提示。只读校验要求 `schema_version = 4`。

备份格式为 6：1.7.0 及更早版本读到会提示「备份格式不受支持」，不写入任何数据，不能拿新备份回退到旧版本使用；本版本仍可读格式 5、4、3 和 2（旧备份没有个人学情、辅导建议与追问记录，恢复后这些表为空）。

## 作答分析

- 入口：学生名单 →「作答分析」；缺测记录不能分析。
- 真实服务先弹确认框，勾选后才发送；请求只含题目、标准解法、学生步骤、单元，不含姓名、学校编号、班级或账号。
- 建议最多 3 条候选错因，每条证据必须能在学生步骤里逐字找到，找不到就丢弃；都不成立时显示「不确定」。
- 建议先标「待核对」：老师可改、可确认、可拒绝；确认才生成正式错题并回写关联，重复确认会被拦住；拒绝不生成错题。
- 模拟模式不发请求；真实服务失败只提示，不自动改用模拟结果。

## 个人学情与辅导建议

- 入口：学生名单 →「个人学情」；页面标题写明班级与学生姓名，只统计这名学生已确认的错题。
- 问题按知识点汇总，没有知识点的归「未分类」；点一条问题可以看到对应错题、原题、学生步骤和日期。
- 加权只用于排序：最近 30 天每次 3 分、31–90 天每次 2 分、更早每次 1 分；缺日期与未来日期单列、不参与加权。
- 老师点「生成辅导建议」才发请求；真实服务先弹确认框，请求只含问题名称、次数、加权分与题目/步骤原文，不含姓名、学校编号、班级名或账号。
- 建议最多聚焦 3 个问题；引用的证据必须能在原文里逐字找到，找不到就丢弃；证据不足时写明不确定。
- 建议可改写、可保存，保存后以老师版本为准；之后相关已确认错题有变化时提示「可能需要更新」。
- 模拟模式下不生成建议、不编造内容。

## 教师短追问

- 两个入口：个人学情里选中一个问题点「追问这个问题」；或打开一条已确认错题点「追问这个问题」。
- 依据区只显示问题名、题目原文、学生过程原文与已确认错因；候选最多 3 条，每条依据必须是原文里的句子，找不到就丢弃；证据不足时写明「证据不足」，不硬给。
- 老师可以点候选直接用，也可以自己写一句；点「记下这次追问」才真正记录提问，之前只是候选。
- 学生答完点「保存学生回答」，会同时记下提问时间与回答时间。
- 「让 AI 更新错因候选」以这次回答为依据，结果标为待核对；老师可以改、可以接受、可以拒绝，接受才会并入这条错题的错因候选，不会自动改写已确认错题。
- 同一学生的追问按时间倒序连续查看，能回到对应错题与个人学情；AI 失败时记录与错题都保持原样。
- 真实服务在发送前弹确认框；请求只含问题名、题目、学生步骤、已确认错因与这次回答，不含姓名、学校编号、班级名或账号。

## 界面

主要窗口统一使用 `src/desktop/theme.py` 的配色与排版：标题、分区、按钮层级一致，主操作、提醒与危险操作分色；表格适当加宽行高，长文本放在可展开的只读框里。

1366×768 这类小屏也放得下：窗口初始尺寸按本机可用屏幕区域收口（`fit_to_screen`）；学生名单的按钮分两行；个人学情、教师短追问与 AI 设置的内容放在可滚动区域，高度不够时出现滚动条，区块不会互相压住。

## 构建

```bat
.venv\Scripts\python.exe tools\build_desktop.py
.venv\Scripts\python.exe tools\pack_source_146.py
```

产物在 `build\程序包\` 与 `build\源码\`。

## 测试

```bat
.venv\Scripts\python.exe tests\verify_answers_150.py
.venv\Scripts\python.exe tests\verify_core_150.py
.venv\Scripts\python.exe tests\verify_review_160.py
.venv\Scripts\python.exe tests\verify_tutor_170.py
.venv\Scripts\python.exe tests\verify_followup_180.py
.venv\Scripts\python.exe tests\verify_ai_146.py
set QT_QPA_PLATFORM=offscreen
.venv\Scripts\python.exe tests\verify_answers_ui.py
.venv\Scripts\python.exe tests\verify_review_ui.py
.venv\Scripts\python.exe tests\verify_tutor_ui.py
.venv\Scripts\python.exe tests\verify_followup_ui.py
.venv\Scripts\python.exe tests\verify_ai_146_ui.py
set QT_QPA_PLATFORM=
.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_answers_exe.py
.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_review_exe.py
.venv\Scripts\python.exe tests\run_hidden.py -- .venv\Scripts\python.exe tests\verify_ai_146_exe.py
```

最终 EXE 测试读取 `build\程序包\数智伴学-<版本>.zip`，在独立 Windows 桌面运行，窗口不显示；日志写入 `docs\`。

---

## 本轮交付（R1 合并版，2026-11-26）

原版 1.8.1 功能保留，R1 能力按需接入，并修复新旧模块之间的数据衔接。**交付入口：**

| 想了解什么 | 看这里 |
| --- | --- |
| 本轮做了什么、怎么核验、测试数字、遗留问题 | [`docs/R1_合并交付汇报.md`](docs/R1_合并交付汇报.md) |
| 从 1.8.1 升级要做什么 | [`docs/迁移说明_1.8.1到R1合并版.md`](docs/迁移说明_1.8.1到R1合并版.md) |
| 接口与状态口径（唯一权威） | [`docs/作答核对_接口契约.md`](docs/作答核对_接口契约.md) |
| 完成定义自检 / 已知欠账 | [`docs/R1-完成定义自检表.md`](docs/R1-完成定义自检表.md) · [`docs/R1-已知欠账清单.md`](docs/R1-已知欠账清单.md) |
| 反例与回归、独立评审 | [`docs/R1-反例与回归记录.md`](docs/R1-反例与回归记录.md) · [`docs/评审_R1合并交付.md`](docs/评审_R1合并交付.md) |
| 打包版端到端结果 | [`docs/verify-exe-回归记录.md`](docs/verify-exe-回归记录.md) |

桌面端新增「作答核对」页与 AI 设置页的「个体数据外发（默认关闭）」开关；批次判定、备份恢复、旧数据只读桥、图片外发等衔接问题均已修复并有对应套件（`tests/verify_r1_*.py`）。
