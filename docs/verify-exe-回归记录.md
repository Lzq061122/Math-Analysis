# 打包版端到端回归记录（T29）

> 目的：把 t9 评审里判为「无法判定」的**打包产物 + 5 个 exe 套件**补成可判定。
> 边界：本轮只装依赖、只跑套件、只写本记录；**未改 `src/**`、未改 `tests/**`、未重新打包**。

## 0. 一句话结论

| 套件 | 项数 | 通过 | 失败 | 退出码 | 耗时 | 判定 |
|---|---|---|---|---|---|---|
| `verify_answers_exe.py` | 28 | 28 | 0 | **0** | 96s | **打包产物 + 隐藏桌面自动化都正常** |
| `verify_followup_exe.py` | 49 | 36 | 13 | **1** | 328s | 测试口径（1.8.1）与 R1-14 闸门冲突，**不是产物问题** |
| `verify_review_exe.py` | 21（跑到 21 崩） | 21 | 0（崩溃） | **1** | 241s | 同上（403 → 等不到「分析建议」窗口） |
| `verify_ai_146_exe.py` | 66（跑到 66 崩） | 65 | 1（+崩溃） | **1** | 332s | 同上（403 → 等不到 `aiSuggestionDialog`） |
| `verify_layout_exe.py` | 80 | 80 | 0 | **0** | 129s | **产物 + 三种屏幕尺寸布局都正常** |

**共同根因**：R1-07 + R1-14 之后「真实通道默认不外发」，教师账号没显式开启时六个个体 AI 入口一律
`403 ai_isolation`。三个红套件写的是 1.8.1 口径（点确认 → 弹建议窗 / 出候选），于是拿不到内容。
**这不是打包产物的问题，也不是交互桌面不可用**——同一次构建里，不依赖个体 AI 的两个套件
（作答导入 28/28、布局 80/80）全程跑通并出了截图，说明 exe 能起、pywinauto 能驱动、截图能落盘。

## 1. 环境与依赖

| 项 | 值 |
|---|---|
| 解释器 | `E:\Agent\venv-desktop\Scripts\python.exe`（Python 3.12.10） |
| 运行方式 | `tests/run_hidden.py -- <python> tests\verify_*_exe.py`（独立 Windows 桌面，窗口不弹到当前桌面） |
| 依赖（安装前） | pywin32 **缺**、pywinauto **缺**、comtypes 缺；Pillow 13.0.0 / PySide6 6.6.3.1 / PyInstaller 6.22.3 已有 |
| 依赖（安装后） | **pywin32 312**、**pywinauto 0.6.9**、comtypes 1.5.0、six 1.17.0（`pip install pywin32 pywinauto` 一并带入） |
| pywin32 后置脚本 | `Scripts\pywin32_postinstall.py -install` 已执行成功（"The pywin32 extensions were successfully installed."） |

import 自证（同一解释器，实测输出）：

```
pywin32 312   pywinauto 0.6.9   comtypes 1.5.0   six 1.17.0
imports OK   pywinauto 0.6.9   win32api.PostMessage? True
```

`import win32con, win32gui, win32process, win32api, pywinauto` 全部成功。

## 2. 交付件指纹（本轮实测，与 captain 报的一致）

| 文件 | 字节 | SHA-256（小写） | 修改时间 |
|---|---|---|---|
| `E:\Agent\小挑\数智伴学\数智伴学.exe` | **2,589,423** | `6f7cbf2195c12cfea4c56c431e7f9afeeaf79325670734107ec4072e1ebc91d7` | 2026-11-25 20:44:39 |
| `E:\Agent\小挑\build\程序包\数智伴学-1.8.1.zip` | 45,999,298 | `bf03667132c76292719697f61494c4c68944fcfc702910bbfc4e314caedd536b` | 2026-11-25 20:44:45 |

**ZIP 路径确认**：5 个套件都用 `ROOT/build/程序包/数智伴学-1.8.1.zip`（`NAME="数智伴学"`、`VERSION` 取自
`src/app/version.py` = `1.8.1`），**就是任务书给的路径，没有写死别的路径，也没有为了跑通去改套件**。
包内 202 个条目、顶层目录 `数智伴学/`、包内 exe 为 `数智伴学/数智伴学.exe`；
`verify_answers_exe.py` 第一节自己复核了 ZIP 存在、包内有 EXE、包内有老师使用说明，三项全 PASS。

## 3. 逐套件结果

### 3.1 `verify_answers_exe.py` — 28/28，exit 0，96s

覆盖：交付件校验 → 解压 → 干净环境启动 EXE → 注册/建班/建学生 → 导入 CSV（预览计数、冲突、覆盖）→
关掉重开确认数据仍在。全部 PASS，说明**打包产物可启动、可建库、可导入、pywinauto 能稳定驱动**。

### 3.2 `verify_followup_exe.py` — 49 项 / 36 通过 / 13 失败，exit 1，328s

失败标题（原样，连续 13 条都在「六、生成候选」「七、记录回答」两节）：

```
FAIL  候选表出现候选  -> 0 个单元格
FAIL  候选带依据原文
FAIL  点选候选行
FAIL  选中候选后填入输入框
FAIL  记下追问后给出提示  -> 没有生成追问候选：「追问候选生成」已停用：正式教师业务流程不向生成式模型发送学生的作答、作答原文、身份或追问原话。本次没有组装请求，也没有把任何内容发出去…
FAIL  数据库里有一条追问  -> 0 条
FAIL  历史表出现这条追问
FAIL  保存回答后给出提示  -> 没有生成追问候选：「追问候选生成」已停用：…
FAIL  回答写入数据库  -> []
FAIL  状态改为已记录回答  -> []
FAIL  记下回答时间  -> []
FAIL  历史里显示已记录回答
FAIL  更新按钮可用
最后崩溃：RuntimeError: 等不到窗口：确认把问题与证据发给
```

判定：**测试口径问题**。前 36 项（登录、AI 设置与两档截图、学生名单、个人学情、追问窗口的依据原文/
不发送身份信息/截图）全 PASS，说明窗口与控件都对；只有「确认后要出候选」这条链断了——403 文案
（「已停用…本次没有组装请求」）由 EXE 状态栏**如实显示**，正是 R1-14 想要的行为。
末尾那次「等不到确认框」是同一个原因的下游表现（第二次点生成时按钮/窗口状态已变）。

### 3.3 `verify_review_exe.py` — 21 项全通过后崩溃，exit 1，241s

```
等不到分析建议，列表状态：分析没有完成（403 ai_isolation）：「作答分析」已停用：…本次没有组装请求…
Traceback: tests/verify_review_exe.py:245 wait_review → RuntimeError: 等不到窗口：分析建议
合计 21 项，通过 21，失败 0；退出码：1
```

判定：**测试口径问题**。确认框的 5 条断言（列出三项发送内容 / 不发送身份 / 未勾选不能开始 /
勾选后可开始 / 没有提前发出请求）全 PASS；随后 EXE 把 403 写进状态栏、**不弹建议窗**，
而套件按 1.8.1 期待「弹建议窗」→ 超时。崩溃行是 `:245`，不是断言失败，报告里按崩溃计。

### 3.4 `verify_ai_146_exe.py` — 66 项 / 65 通过 / 1 失败后崩溃，exit 1，332s

```
FAIL  分析失败时照片与录入内容保留 ｜ 分析失败：「错题照片分析建议」已停用：…
最后崩溃：tests/verify_ai_146_exe.py:552 → RuntimeError: 等不到控件：aiSuggestionDialog（窗口 '数智伴学 · 错题'）
```

判定：**测试口径问题**。该套件验的是「错题照片分析建议」（AI 设置页、试分析以外的个体 AI 入口），
R1-14 下同样 403：界面把原因写进对话框状态，但不弹 `aiSuggestionDialog` 建议窗。
注意：**错题表、照片、录入内容保留**这类与 AI 无关的断言都 PASS，说明错题链路本身是好的。

### 3.5 `verify_layout_exe.py` — 80/80，exit 0，129s

三种屏幕（1366×768 / 1920×1080 / 1366×768@125%，按 `QT_SCALE_FACTOR` 调成与目标一致的缩放再摆窗口）
全绿，12 张截图落在 `docs/screenshots/1.8.1-exe/`（AI 设置、个人学情、短追问 × 三档尺寸 + 底部滚动图）。
说明：小屏适配、控件不溢出、窗口不出屏外这三件事在**打包产物**上都成立。

## 4. 失败归因：产物问题 还是 测试环境问题？

| 候选原因 | 是否成立 | 证据 |
|---|---|---|
| 打包产物缺文件 / 起不来 | **不成立** | `verify_answers_exe` 28/28（含「包内有 EXE/说明」与关掉重开）、`verify_layout_exe` 80/80 都跑通同一份 exe |
| 缺依赖（pywin32/pywinauto） | **不成立** | 本轮已装并 import 自证；五个套件都跑起来了，没有一个是 import 失败 |
| 需要交互桌面 / 无法自动化 | **不成立** | `run_hidden.py` 建独立桌面成功，pywinauto 找到并操作了登录窗、班级窗、学生名单、作答分析、追问、AI 设置、错题等窗口；两个绿套件还落了 12+ 张截图 |
| **个体 AI 默认不外发（R1-14）与 1.8.1 口径套件冲突** | **成立** | 三条红的失败文本都是 `403 ai_isolation`／「…已停用：…本次没有组装请求」；同一次构建里不依赖个体 AI 的两个套件全绿 |
| 缺 zip | 不成立 | zip 存在且与套件期望路径一致（§2） |

## 5. 建议（交给 captain 派单，本轮不自行处理）

1. **exe 套件按 t26 口径改写**（三个：followup / review / ai_146）：夹具里先把**该 EXE 自己数据目录**里的
   账号开关置为开启（桌面 UI 已有「个体数据外发（默认关闭）」入口，`src/desktop/ui.py:4289`；
   也可以在启动前按 `DesktopService.set_ai_individual(True)` 的同一口径写库），再验原功能；
   另加一组「关闭时六入口 403 `ai_isolation`、文案含「已停用」」。
2. 改写时顺手处理两处**崩溃 -> 断言**：`verify_review_exe.py:245 wait_review`、
   `verify_ai_146_exe.py:552 aiSuggestionDialog`，把「等不到窗口」写成显式空态断言
   （与 t26 在源码级套件做的 followup:315 / review:224 同一种处理）。
3. 本记录里的失败**不应记入 R1 业务回归**：R1 的源码级回归（21 套件 1949 项）已由 t7 与之后的全量跑确认；
   这里是 exe 层套件的口径欠账。

## 6. 复跑方法

```powershell
$py = "E:\Agent\venv-desktop\Scripts\python.exe"
cd E:\Agent\小挑
& $py tests\run_hidden.py -- $py tests\verify_answers_exe.py     # 28/28  exit 0  96s
& $py tests\run_hidden.py -- $py tests\verify_followup_exe.py    # 49 项 36/13 exit 1 328s
& $py tests\run_hidden.py -- $py tests\verify_review_exe.py      # 21 项后崩 exit 1 241s
& $py tests\run_hidden.py -- $py tests\verify_ai_146_exe.py      # 66 项 65/1 后崩 exit 1 332s
& $py tests\run_hidden.py -- $py tests\verify_layout_exe.py      # 80/80  exit 0 129s
```

五个套件串行跑完约 **19 分钟**（单个 96–332 秒，主要花在解压 zip + 启动 PyInstaller exe + 控件等待）。
不要并发跑：每个套件都会解压到 `tests/.tmp-test/*` 并各起一个 EXE 进程，并发会抢窗口与数据库锁。

## 7. 原始证据位置

| 内容 | 位置 |
|---|---|
| 五个套件的完整输出 | `docs/verify-answers-exe.txt`、`verify-followup-exe.txt`、`verify-review-exe.txt`、`verify-ai-exe.txt`、`verify-layout-exe.txt` |
| 布局套件截图（12 张） | `docs/screenshots/1.8.1-exe/` |
| 追问/AI 设置截图 | 套件自写 `docs/screenshots/1.8.0-exe/`（如存在） |
| 临时解压与临时库 | `tests/.tmp-test/answers-exe-*`、`followup-exe-*`、`review-exe-*` 等（跑完不删，便于复查） |
| 本轮驱动的原始控制台输出 | `C:\Users\Public\t29-verify\stdout-*.log`、`exe-summary.txt`（不入库） |

---

## 8. 2026-11-25 新构建回归（t31 修复之后）

**本次跑的是新构建**（不是 11/19 那个包）。旧构建已移到 `E:\Agent\_旧构建_1.8.1_1119b`。

| 产物 | sha256（前 32 位） | 大小 | 修改时间 |
|---|---|---|---|
| `数智伴学\数智伴学.exe` | `7C6C4F2F02F732538446AA00C2EE20CA…` | 2591095 B | 2026-11-25 22:20:19 |
| `build\程序包\数智伴学-1.8.1.zip` | `6FAB7CF8507ECE8B5F5A4F9C24A68BCF…` | 46003273 B | 2026-11-25 22:20:27 |

跑法：`E:\Agent\venv-desktop\Scripts\python.exe tests\<套件>.py`，**串行**执行（每个套件会自己解压 zip 到 `tests/.tmp-test/*` 并起一个 exe 进程，并发会抢窗口与数据库锁）。原始输出在 `C:\Users\Public\t32-exe\*.log`（不入库）。

| 套件 | 项数 | 通过 | 失败 | 退出码 | 耗时 | 上一轮对比 |
|---|---|---|---|---|---|---|
| `verify_answers_exe.py` | 28 | 28 | 0 | 0 | 54.8s | 28/28（持平） |
| `verify_followup_exe.py` | 105 | 105 | 0 | 0 | 135.0s | 105/105（持平） |
| `verify_review_exe.py` | 55 | 55 | 0 | 0 | 118.2s | 55/55（持平） |
| `verify_ai_146_exe.py` | 89 | 89 | 0 | 0 | 177.8s | **68 项 / 66 通过 / 2 失败 → 全绿** |
| `verify_layout_exe.py` | 80 | 80 | 0 | 0 | 85.5s | 80/80（持平） |
| **合计** | **357** | **357** | **0** | 0 | 约 9.5 分钟 | — |

### 8.1 上一轮那 2 条失败已转绿（照片链路）

上一轮 `verify_ai_146_exe` 的 2 条失败是「错题照片分析发不出去」（`error_code=payload`、桩收到 0 次）。本轮同类证据已变绿：

- `PASS 试分析确实发出一次请求 ｜ 1`
- `PASS 请求实际包含图片 ｜ {'model': 'qwen3.8-max', 'has_image': True, ...}`
- `PASS 自定义请求实际包含图片 ｜ {'model': 'vision-x', 'has_image': True, ...}`
- `PASS 请求文字不含学生姓名 ｜ 未出现学生姓名`
- 未授权侧仍按原口径拒绝：`PASS 八.2 错题照片分析建议 → 403 ai_isolation`、`PASS 八.3 错题重新分析（旧入口同一道闸门）→ 403 ai_isolation`

注：该套件本轮 **89 项**（上一轮 68 项），是套件自身扩充了「开关开启后图片实际发送 + 审计 + 未授权仍拒」的断言，属预期增长，不是漏测。

### 8.2 临时产物清理

- t30 遗留的两个临时文件 `tests/.t30_mutant_followup_exe.py`、`docs/.t30-mutant-followup-exe.txt`：**已不存在**（本轮核对 `Test-Path` 均为 False），无需再删。
- 各套件运行时生成的 `tests/.tmp-test/*`、`tests/.tmp-*` 属运行期解压/临时库，**不入库**；本轮跑完已清掉残留的 `tests/.tmp-*`（含历次变体目录）。
- 今后**变异体脚本一律放 `C:\Users\Public\`**，不落仓库。
