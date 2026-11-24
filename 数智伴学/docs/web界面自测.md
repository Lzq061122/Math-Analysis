# 教师端网页「作答核对与追问」界面自测（T2 建立 → T10 修复）

**范围**：`src/web/app.js`、`src/web/styles.css` 的 A1–A6 改动，以及 **T10 修复轮**（走查硬指标：A7 演示入口、用词分离、空状态、单提交、toast、回执首屏、记录表细节）的界面变更。
**轮次**：T2 建界面 → T10 按 `docs/独立走查记录.md` 修硬指标。**本文已按 T10 后的现状校订**；凡 T2 当时的测量值都标了「T2 时」，不要当现状引用。
**依据**：`docs/作答核对_接口契约.md` §3（JSON 键名）、§4（路由与文案）；`数智伴学_修改任务清单.md` A1–A6；`答辩交互样稿_从错步到追问.html`（交互与按钮层级思路）。
**不在本轮范围**：B1–B7、C；`src/desktop/`、`src/server.py`、`src/app/**`、`tests/**` 一行未改。

---

## 1. 路由与导航

| 路由 | 页面 | 对应项 |
| --- | --- | --- |
| `#/classes` | 班级列表（「进入班级」→ `#/class/{id}/check`） | A1 入口 |
| `#/class/{id}/check` | 课前总览（作答核对入口，进入班级的默认页） | A1 |
| `#/class/{id}/check/{answer_id}` | 作答核对（首处错步 → 保存错步 → 当面追问 → 同屏回执） | A2 A3 A4 A5 |
| `#/class/{id}/check/records?cause_state=undecided\|decided\|all` | 核对记录（按原因判断找回） | A5 |

- 班级页 tab 顺序：**作答核对**、概况、学生、错题、统计、教学建议（原五个 tab 的 URL 与渲染函数未动，仅新增首个 tab）。
- `#/class/{id}`（不带 tab）现在默认落到 `check`，与契约「进入班级的默认页」一致；`#/class/{id}/overview` 等旧地址照常可用。
- 解析方式沿用原 hash 路由：`renderClass(classId, tab, sub, query)`，`sub` 是 tab 内的子页面（`records` 或 `answer_id`）。
- **回执不是独立路由**：保存成功后仍在 `#/class/{id}/check/{answer_id}`，当前页内容切换为「结果区」（`wrapper.replaceChildren(receipt)`），`location.hash` 不变。

---

## 2. A1–A6 落点

| 项 | 落点（页面/元素） | 文案（逐字） | 代码位置 |
| --- | --- | --- | --- |
| A1 | 课前总览 hero 卡 | 主按钮按状态给出**唯一一个可点的下一步**：`has_pending` → 「查看待核对作答」（`a.btn.primary`，`href=#/class/{id}/check/{next.answer_id}`，一次点击直达）；`no_answers` → 「导入作答」（`href=#/upload?class={id}`）；`all_checked` → 「查看核对记录」 | `renderCheckOverview` |
| A1 | 同上 | `state==="no_answers"` → 标题「尚未导入作答」+ 一句说明，无表格、无 flag、不渲染「当前作业：…」行（作业名为空或 `未命名批次` 时整行不渲染）；`state==="all_checked"` → 标题「本次作业已核对完」，作业表不为空 | 同上 |
| A1 | 同上 | 次级入口「新建班级」「AI 设置」「导入作答」「刷新」为小号按钮（`btn sm`），不带 `.btn.primary` | 同上 |
| A1 | 用词分离（T10） | 错题侧计数一律带「错题」二字：班级卡「待确认错题 / 已确认错题」，概况 KPI 同口径，错题页签「待确认错题」；作答核对页只用「待核对作答 / 已核对」。班级列表页不再出现孤立的「待核对」 | `renderClasses` / `renderOverview` / `renderCheckOverview` |
| A1 | 作业列表 | 多作业切换（`?batch=`），每行显示应交/已核对/待核对作答 | 同上 |
| A7 | 课前总览「合成演示数据」卡（T10 新增） | 「载入合成演示数据」「重置合成数据」两个按钮 + 同行「只影响合成数据，真实导入的作答不受影响」+「演示不需要选文件、配模型或等网络」；成功后显示 `seeded.answers` / `deleted.* + kept.imported_answers` 数字并立即刷新本页；失败显示错误横幅 | `runDemo` |
| A2 | 左列「学生原文（逐行）」 | 错步行 `<u>` 下划线 + 文字标记「首处不等价」（下划线只包 `hint_text`，如 `-2`）；**整页「首处不等价」只出现这一处**（T10 删掉了卡片标题旁重复的那枚） | `checkStepText` / `checkLineRow` |
| A2 | 同上「对照」区 | 「按规则应为 3x-6 / 学生这一行 -2」+ 契约 `contrast.note` | `checkContrast` |
| A2 | 同上「标准解」 | `<details>` 默认折叠，摘要「标准解（默认折叠，点击展开）」 | `checkStdSolution` |
| A2 | 画面上方 | `locate.source==="preset"` → 「预置演示 · 不代表已实现通用定位」 | `renderCheckAnswer` |
| A2 | 状态文案 | `correct`/`missing`/`missing_steps`/`out_of_scope` 各自一句「为什么不标错」，不猜原因 | `checkLocateNotice` |
| A3 | 右列三区块 | 标题逐字「观察到的错步」「可能原因」「教师判断」 | `renderCheckAnswer` |
| A3 | 底部固定操作栏 | **只有一个提交按钮**，逐字「保存错步，原因待判断」（保存中显示「保存中…」，成功/失败复原） | `actionBar` |
| A3 | 「可能原因」卡内 `.adopt-row`（T10 移动） | 「采纳选中原因并保存」跟着候选走：选中某条候选后才可点，说明文字就在按钮旁；**它不在底部操作栏里**，不与主按钮并排 | `causeBlock` |
| A3 | 操作栏 + 回执 | `progress` 与 `cause_state` 两个 `.axis-chip` 分开渲染，中间只有一个分隔串（「待核对 · 原因待判断」） | `checkAxisChips` |
| A4 | 「当面追问」卡 | 「可以这样问学生：你当时是怎么展开这个括号的？」 | `askCard` |
| A4 | 同上 | 只有两个动作：「记录学生回答」「暂时跳过」；前者展开原话输入框 | 同上 |
| A4 | 同上 | 「暂时跳过」只改本地状态：「已跳过提问，未记录回答」；**不发请求、不跳页、不保存** | `skipBtn` 回调 |
| A5 | 回执区 | 「已保存」+「已保存到：{班级} · {作业}」+ 两轴状态 + **保存时间显示为 `YYYY-MM-DD HH:MM`**（例 `2026-11-24 14:46`，不再出现 ISO 串）+ 学生原话 + 支持/削弱依据 + 教师判断 | `showReceipt` / `fmtDateTime` |
| A5 | 回执区（T10） | 主动作放进 sticky 底部操作栏：非最后一份 → 「查看下一份」→ `#/class/{id}/check/{queue.next_answer_id}`；最后一份 → 「返回课前总览」+ 收尾文案「本次作业的待核对作答已全部处理完」 | 同上 |
| A5 | 失败 | 409/500/网络错误：留在当前页、输入全保留、错误提示在固定操作栏、按钮文案复原可重试；409 额外给「刷新这份作答」链接 | `fillError` / `save` |
| A5 | 记录页 | 三条筛选（原因待判断/教师已判断/全部）、表格含「**作业批次**」列（契约 §3.4 `items.batch`）、保存时间同样本地化、空筛选有说明、行内「打开」是按钮、主按钮「查看待核对作答」 | `renderCheckRecords` |
| A6 | 全部 | 1366×768 无横向滚动；每屏只有一个 `.btn.primary`；错步不只靠颜色（下划线 + 文字标记） | `styles.css` `.check-*` / 各视图 |
| A6 | 跨页提示（T10） | `clearToast()` 在每次 `doRender` 与回执同屏渲染时清空提示，「已跳过提问，未记录回答」不再跟到回执页/记录页 | `clearToast` |

**A2 字号对照**：`.step-math` 22–30px（1366 宽实测 30px）对 `.rec-no`（记录编号）12px，实测比值为 2.5，符合「公式比记录编号醒目」。
**A6 首屏测量**（iframe 内 1366×768 视口，T10 复测）：详情页错步行底边 606px、底部操作栏里的主按钮 715–757px；回执页「查看下一份」715–757px（T2 时它在 1003px、需要滚动，T10 已修）；各页 `scrollWidth == clientWidth`。

> **仍待坐标确认的一点**：滚动到页面中段时，sticky 操作栏会盖住正文最后一行（滚到底可以露出，不丢内容）。这属观感问题，已交 verify 在 T11 用「最大滚动位置下正文末元素 bottom ≤ 操作栏 top」的坐标断言判定；若不过，改法是给正文容器加与操作栏等高的底部留白，**不是**改操作栏高度。本文不预判结果。

---

## 3. 前端读取的契约键名（只读 §3，不新增字段）

| 接口 | 读取的键 |
| --- | --- |
| `GET .../check/overview` | `state`, `assignment.{batch,name,total,pending,checked,missing}`, `assignments[]`, `next.{answer_id,student_label,question_no,headline}` |
| `GET .../check/answers/{id}` | `answer.{id,record_key,batch,question_no,question_text,unit,standard_solution,student_steps,occurred_on,missing,missing_reason,student_label,data_source,data_source_label,version}`, `locate.{status,source,first_bad_index,first_bad_text,underline.{start,end},hint_text,contrast.{expected,actual,note},reason_note,manual_note,lines[].{text,role,bad}}`, `candidates[].{code,label,support,rebut,pending,selected}`, `causes_source`, `causes_note`, `judgement.{progress,cause_state,progress_label,cause_label,cause_label_text,observed_step,cause_code,teacher_note,saved_at,stale}`, `followup.{question,recorded,answer_text}`, `queue.{position,total,is_last,batch}` |
| `POST .../save` | 发送 `expected_version`, `observed_step`, `first_bad_index`, `cause_code`, `cause_label`, `teacher_note`, `cause_state`, `followup.{question,answer_text}`；读取返回 `receipt.{class_name,assignment_name,saved_at,status_line}`, `judgement`, `queue.{next_answer_id,is_last,remaining}` |
| `GET .../check/records` | `total`, `items[].{answer_id,student_label,question_no,observed_step,progress,cause_state,cause_code,cause_label_text,saved_at,batch}` |

> 注意命名差异：`judgement.cause_label` 是「原因待判断／教师已判断」这条轴的标签，`judgement.cause_label_text` 才是教师选中的原因名；保存请求里的 `cause_label` 取的是后者。代码按此分开读取。

---

## 4. 缺字段时的兜底（按契约默认值，不猜新字段）

| 缺什么 | 界面怎么处理 |
| --- | --- |
| `overview.state` 缺失 | 按 `no_answers` 显示「尚未导入作答」 |
| `assignment` 及 `total/pending/checked/missing` 缺失 | 数值按 0；总览里的作业名留空即整行不显示，回执里回落到 `answer.batch` 或「本次作业」 |
| `next` 缺失 | `has_pending` 有数据但没有 next 时，主按钮退回「查看核对记录」，不伪造作答；`no_answers` 时主按钮是「导入作答」 |
| `answer.data_source` 缺失 | 标为「真实导入」（不把来源不明的作答说成合成演示） |
| `answer.record_key` 缺失 | 记录编号显示 `#<answer_id>` |
| `locate.lines` 缺失 | 用 `answer.student_steps` 按行兜底，首行记「原题」，其余「第 N 行」，不标错步 |
| `locate.status` 缺失/未知 | 按人工核对处理：原步骤照常显示，不标错步，给一句说明 |
| `locate.hint_text`/`underline` 缺失或与原文对不上 | 整行加下划线，仍保留下划线 + 文字标记「首处不等价」 |
| `judgement` 缺失 | `progress="pending"`、`cause_state="undecided"`，标签「待核对 · 原因待判断」 |
| `followup.question` 缺失 | 用默认问句「你当时是怎么展开这个括号的？」 |
| `candidates` 为空 | 显示 `causes_note`；再缺则「尚无已审错因库可用，保留待判断。」 |
| `queue` 缺失 | 不显示队列行，回执里「查看下一份」按 `next_answer_id` 是否存在决定 |
| `receipt` 缺失或字段不全 | 班级名用当前班级、作业名用 `answer.batch`；`status_line` 缺则只渲染两轴标签 |
| 记录行的 `batch` 缺失 | 「作业批次」列显示「—」，不影响其它列 |
| 记录行 / 回执的 `saved_at` 缺失或无法解析 | 显示「—」；能解析时本地化成 `YYYY-MM-DD HH:MM`（不把 ISO 串直接给老师看） |
| `overview.state==="no_answers"` 但 `assignment.name` 是占位值（如「未命名批次」） | 「当前作业：…」整行不渲染，避免让人以为要先建作业 |

以上兜底均在界面上以契约文案呈现，没有新增后端字段，也没有把缺失字段当作「已完成」。

---

## 5. 与契约/任务清单的取舍（请 lead 复核）

1. **主保存按钮放在底部固定操作栏（sticky）**，不是放在「教师判断」卡里。原因：契约 §4/A6 要求 1366×768 首屏同时看到「错步」与「主动作」，把按钮留在第三区块会被折叠到首屏之外。文案、数量（每屏一个 `.btn.primary`）均不变。
2. **班级页「＋ 上传错题」在「作答核对」tab 下降为 `btn sm`（无 primary）**。否则班级页头部会与主按钮并列出现第二个 `.btn.primary`，违反 A6。
3. **班级列表页「＋ 新建班级」降为次级按钮**（A1 口径「新建班级、AI 设置保留为次级入口」）；班级卡片内「进入班级」仍为 `primary`。
4. **「可能原因」本轮可选中但不自动采纳**：要写入教师已判断，用次级按钮「采纳选中原因并保存」（`cause_state="decided"` + `cause_code`/`cause_label`）。该按钮在「可能原因」卡的 `.adopt-row` 里，跟候选就近（T10 从底部栏移入，见走查发现 ④）。
   - 早期版本这里有个坑：主按钮永远写「保存错步，原因待判断」，但已判断的作答再点它会发 `decided`——按钮在撒谎，而且没有回到 `undecided` 的路。**该问题（T5 评审 R-04）已按契约 §4.2 修掉，见第 13 条与 §6.3。**
5. **`#/class/{id}` 默认 tab 改为作答核对**（原为概况）；旧 tab 地址全部保留可用。
6. **接口缺失/报错时只显示错误横幅**，不用假数据填充；非 `first_bad` 状态一律不标错步。
7. **（T10）错题与作答用词分开**：难处在于班级卡与作答核对页都写「待核对」，但一个指错题、一个指作答。现在错题侧一律「待确认错题 / 已确认错题」，作答核对页「待核对作答 / 已核对」；依据 `docs/独立走查记录.md` 发现 ②。
8. **（T10）`no_answers` 主按钮改成可点的「导入作答」**：原来是 disabled 的「查看待核对作答」，等于把下一步藏起来（发现 ③）。现在按状态给出唯一一个可点下一步：导入作答 / 查看待核对作答 / 查看核对记录。
9. **（T10）合成演示数据有界面入口**：总览常驻「合成演示数据」卡（载入 / 重置 + 「只影响合成数据」+ 结果数字）。这是 A7「演示无需现场选文件、配模型或等网络」在界面上唯一的落点（发现 ①）。
10. **（T10）记录表补「作业批次」列、保存时间本地化、行内「打开」改按钮**（发现 ⑨⑩⑪）。
11. **（T10）「首处不等价」整页只留一处**：只标在错步行上，卡片标题旁那枚删掉，免得被理解成「标记整段」（发现 ⑫）。
12. **（T10）toast 跨页清除**：提示属于上一屏的动作结果，`clearToast()` 在切页与回执同屏渲染时清掉（发现 ⑤）。
13. **（T14 / R-04）主按钮文案随原因状态走 + 补「改为原因待判断」**：按契约 §4.2 的对照表——
    - `cause_state="undecided"`（或 `progress="pending"`）：主按钮逐字「保存错步，原因待判断」，发 `cause_state="undecided"`；A3 原文要求的文案由这一支承担，没有删。
    - `cause_state="decided"`：主按钮改为**「保存错步（沿用已判断原因）」**，仍发 `cause_state="decided"` + 原 `cause_code`/`cause_label`（沿用原结论，不被打回待判断）；同时「教师判断」卡里显示「当前原因：…」并给次级动作**「改为原因待判断」**（`btn sm`，非 primary），点击发 `cause_state="undecided"` 且不带 `cause_code`，旧原因由后端推进 `cause_history`。
    - 两条额外动作都是非 `.btn.primary`，每屏仍只有一个主按钮；底部操作栏仍只有一个提交按钮。

---

## 6. 已执行的验证

```
node --check src\web\app.js
→ 通过（exit 0）

python -c "…11 条文案断言…"
→ 文案齐备 11

# T10 追加的文案断言（8 条）：载入合成演示数据 / 重置合成数据 / 只影响合成数据 /
# 导入作答 / 待确认错题 / 查看下一份 / 保存错步，原因待判断 / 采纳选中原因并保存
→ 文案齐备 8；另「未命名批次」检查：检查完毕
```

界面自测（T2 轮，临时夹具，**不在仓库内**）：用 Node 内置 `http` 起一个按契约 §3 造数的本地服务（`127.0.0.1:8877`，路径 `%TEMP%\shuzhi-web-harness\server.js`），把真实 `index.html`/`app.js`/`styles.css` 放进 1366×768 的 iframe 里跑脚本化点击，**68 条断言全部通过**，关键结果：

- 路由与导航：`进入班级 → #/class/3/check`；tab 序列 `作答核对,概况,学生,错题,统计,教学建议`；`#/class/{id}` 默认落到作答核对；概况/学生/错题/统计/教学建议五个 tab 均能进入各自渲染器（夹具未实现的后端接口只显示错误横幅）。
- A1（T2 时）：主按钮文案「查看待核对作答」、`href=#/class/3/check/12`、数量 1；`no_answers` 有「尚未导入作答」且页面无 `<table>`；`all_checked` 有「本次作业已核对完」且表格不为空。**T2 时两种「没有待核对」状态下主按钮是 `disabled`，T10 已改为可点的「导入作答」，见 §6.2。**
- A2（T2 时）：「首处不等价」出现 2 处（行内标记 + 区块头），错步行 `<u>` 内容恰为 `-2`（`hint_text` 逐字），`<details>` 默认折叠，`预置演示 · 不代表已实现通用定位` 存在，`.step-math` 30px vs 记录编号 12px。**T10 起整页只出现 1 处，见 §6.2。**
- A3：三个区块标题逐字命中；主按钮文案逐字命中；`.axis-chip` 两个独立元素，拼起来的文本是「待核对·原因待判断」。
- A4：问句逐字命中；追问卡内 `button` 恰好 2 个；点「暂时跳过」前后**请求数 23 → 23**（零请求）、hash 未变、出现「已跳过提问，未记录回答」、原话输入区隐藏、没有出现「已保存到：」。
- A5：保存后 hash 仍是 `#/class/3/check/12`，同屏出现「已保存到：七年级 1 班 · 9月第2次作业」、学生原话、支持/削弱、教师判断；「查看下一份」→ `#/class/3/check/13`（T10 起该按钮在底部操作栏里，首屏可见）；最后一份保存后出现「已全部处理完」且主动作变「返回课前总览」；409 时留在本页、两处输入（原话、备注）原样保留、错误提示可见、主按钮文案复原、出现「刷新这份作答」链接。
- A6：三种状态与作答页 `scrollWidth == clientWidth`（1349/1349、1366/1366），无横向滚动；作答页首屏内可见错步行与操作栏；每屏 `.btn.primary` 计数均为 1。

视觉核对（同一 1366×768 iframe 视口截图）：课前总览三态、作答核对页（含下划线错步行与底部操作栏）、保存回执、核对记录页均按上述布局渲染。

### 6.1 T10 修复轮复测（走查硬指标）

同一套临时夹具（新脚本 `%TEMP%\shuzhi-web-harness\server_t10.js`，仍不入库）在**真 1366×768 iframe 视口**内脚本化点击，两轮共 **58 条断言全部 PASS**：

- **布局/交互 45 条**：班级列表页「待确认错题」命中且孤立的「待核对」计数 0；总览主按钮「查看待核对作答」计数 1；「载入合成演示数据」「重置合成数据」「只影响合成数据」齐备；空状态主按钮 `tagName=A`、文案「导入作答」、`href=#/upload?class=5`、`disabled=false`；空状态无「当前作业」「未命名批次」；点载入后出现「已载入合成演示数据：新增 4 份」+ 只影响合成数据 + 数据已刷新；后端 500 时出现横幅「载入合成演示数据失败：磁盘写入失败。」；`.check-bar` 内 button 计数 1（保存错步，原因待判断）；采纳按钮 `closest('.check-bar')=null`、位于候选卡；整页「首处不等价」计数 1；跳过→保存后 toast 文本 `""`；回执「已保存到：」命中、保存时间匹配 `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$`、无 `+08:00`；回执「查看下一份」矩形 top=715 / bottom=757（视口 768）；记录表头含「作业批次」、值「9月第2次作业」、行内「打开」是 button；各页 `scrollWidth==clientWidth`（1349/1349、1366/1366）、`.btn.primary`=1。
- **采纳 / 重置 / toast 13 条**：选中第 2 条候选后采纳按钮启用；点击后 `POST …/save` 的请求体实测 `cause_state=decided`、`cause_code=E-DIST-02`、`cause_label=知道规则，这次漏写`，回执显示「教师已判断」（**后端口径未变**）；重置显示「已重置合成数据：删除 4 份作答、1 条追问；真实导入的作答保留 2 份」；跳过后的 toast 在切到记录页时变 `""`。
- **回归**：`python tests\verify_answer_check_150.py` → 127/127、失败 0、退出码 0；三条既有回归线 60/60、126/126、134/135（唯一失败仍是已知的历史版本号断言）。
- **截图**：`docs/screenshots/ui-fix-empty.png`（空状态 + 演示入口）、`docs/screenshots/ui-fix-receipt.png`（回执 + 首屏可见的「查看下一份」）。两张都是在真 1366×768 iframe 视口内渲染后裁到页面区域；为适配本机窗口高度，截图时对 iframe **元素**用了 0.88 缩放，iframe 内部视口尺寸与布局未变。

### 6.2 与真实后端核对键名（临时库，`127.0.0.1:8878`）

用 `C:\Python314\python.exe src\server.py --port 8878 --data-dir <临时目录>` 起真实服务，注册测试账号 → 建班 → `POST check/demo/seed` → 逐个接口比对**网页端实际读取的键**，结果 `ALL_KEYS_OK`：

- `overview`：`state / assignment{batch,name,total,pending,checked,missing} / assignments[]{…} / next{answer_id,student_label,question_no,headline} / counts` 全部存在；实测 `state=has_pending`。
- `answers/{id}`：`answer`(15 键)、`locate`(10 键，含 `contrast`、`lines[].{text,role,bad}`)、`candidates[].{code,label,support,rebut,pending}`、`judgement`(10 键)、`followup`(3 键)、`queue`(4 键) 全部存在；案例 A 实测 `status=first_bad, source=preset, hint_text="-2", underline={start:2,end:4}` 且 `hint_text in first_bad_text`。
- `save`：`receipt{class_name,assignment_name,saved_at,status_line}`（实测 `status_line="已核对 · 原因待判断"`）、`judgement`、`queue{next_answer_id,is_last,remaining,total}` 全部存在。
- `records?cause_state=undecided`：`total` + `items[].{answer_id,student_label,question_no,observed_step,progress,cause_state,cause_code,cause_label_text,saved_at,batch}` 全部存在。

> 说明：这次只探接口键名与保存/记录返回，没有在浏览器里跑真实后端登录（本机鼠标/窗口类操作需要人工同步，本轮不可用）；真实链路的端到端留给 verify 成员的 8899 环境。

### 6.3 T14 / R-04：三段实跑（undecided → 采纳 → decided 主按钮 → 改为原因待判断）

**夹具脚本（`server_t14.js`，不入库，1366×768 iframe 内脚本化点击）23 条断言全 PASS**，逐步真实值：

| 步 | 主按钮文案（实测） | 发出的请求体（关键字段） | 回执两轴（实测） |
| --- | --- | --- | --- |
| ① `undecided` 作答点主按钮 | 「保存错步，原因待判断」 | `cause_state:"undecided"`、`cause_code:""`、`cause_label:""` | 已核对 · 原因待判断 |
| ② `decided` 作答点主按钮 | 「保存错步（沿用已判断原因）」 | `cause_state:"decided"`、`cause_code:"E-DIST-01"`、`cause_label:"只把系数乘以括号里的第一项"` | 已核对 · 教师已判断 |
| ③ `decided` 作答点「改为原因待判断」 | 主按钮文案不变（仍是「沿用已判断原因」） | `cause_state:"undecided"`、`cause_code:""`、`cause_label:""` | 已核对 · 原因待判断 |

同轮断言还包括：`undecided` 作答页**没有**「改为原因待判断」按钮；`decided` 页主按钮文本里不含「原因待判断」；「改为原因待判断」的 className 是 `btn sm`（**非** `.btn.primary`）；每屏 `.btn.primary` 计数 = 1；`scrollWidth == clientWidth`（1348/1348，无横向滚动）；回到 `undecided` 的作答后主按钮文案复原为「保存错步，原因待判断」。

**真实后端（`python src\server.py --port 8879 --data-dir <临时库>` + `POST check/demo/seed`）**：

1. 采纳保存 `decided` → `judgement.cause_state="decided"`、`cause_code="E-DIST-01"`、`cause_label_text="只把系数乘以括号里的第一项（常数项没乘）"`。
2. 「改为原因待判断」保存 `undecided` → 返回 `receipt.status_line="已核对 · 原因待判断"`，`judgement` 回到 `cause_state="undecided"`、`cause_code=""`、`cause_label_text=""`。
3. 临时库 `answer_checks`（同一行 id=1）：
   `cause_state=undecided`、`cause_code=''`、`cause_history='[{"code":"E-DIST-01","label":"只把系数乘以括号里的第一项（常数项没乘）","decided_at":"2026-11-24T15:30:29+08:00"}]'`
   → 旧原因进 `cause_history`，当前结论已清干净。

**回归**：`python tests\verify_answer_check_150.py` → **133/133、失败 0、退出码 0**（用例数比 T10 时的 127 多，是后端这轮新增了 R-04/R-07 相关断言，不是前端跑红）。

---

## 7. 未覆盖 / 交给 E2E 验收（verify 成员）

- 需要真实后端（`python src\server.py --port 8899`）跑通 `overview / answers / save / records` 与 409 并发冲突，本页只按契约键名取数，未做任何桩数据。
- 保存成功后的「刷新页面仍显示 已核对 · 原因待判断」（A3 完成标准里的「关闭重开」）依赖后端持久化，需在 E2E 里断言。
- **T10 之后的聚焦复验（T11）由 verify 对当前字节取证**；本文所有坐标类结论（首屏位置、无横向滚动、`.btn.primary` 计数）都以 verify 的实测为最终口径。
- **一处未定案（不是已解决）**：sticky 操作栏在滚动到中段时会盖住正文最后一行，滚到底可以露出。verify 正在用「最大滚动位置下正文末元素 bottom ≤ 操作栏 top」的坐标断言判定；若不过，改法是给正文容器加与操作栏等高的底部留白，不是压低操作栏。本文不替它下结论。
- 桌面端（`src/desktop/`）不在本轮交付，未改一行。
- 本轮追问只记录一条原话（`followup.answer_text`），学生多次作答/拒答区分属于 B3。
