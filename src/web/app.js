/* 教师端数学错题学情分析 —— 单页应用（原生 JS，无构建步骤） */

'use strict';

const $app = document.getElementById('app');
const state = {
  user: null,
  meta: null,
  // checkDrafts：作答核对页里老师正在填的内容（错步、备注、学生原话）。
  // 保存失败时页面不重渲染，输入本来就还在；这里再留一份，防止意外重渲染把输入冲掉。
  ui: { statsFilters: {}, mistakeFilters: {}, drill: null, checkDrafts: {} },
};

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */
function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'value') node.value = value;
      else if (key === 'checked') node.checked = !!value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
  }
  appendKids(node, kids);
  return node;
}

function appendKids(node, kids) {
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    if (Array.isArray(kid)) appendKids(node, kid);
    else if (kid instanceof Node) node.appendChild(kid);
    else node.appendChild(document.createTextNode(String(kid)));
  }
}

let toastTimer = null;
function toast(message, isError) {
  const box = document.getElementById('toast');
  box.textContent = message;
  box.className = 'show' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.className = ''; }, 2600);
}

// 跳页/换屏时把上一屏的提示清掉：提示属于上一屏的动作结果，跟着跑到回执或记录页会误导。
function clearToast() {
  const box = document.getElementById('toast');
  clearTimeout(toastTimer);
  toastTimer = null;
  if (box) {
    box.className = '';
    box.textContent = '';
  }
}

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', credentials: 'same-origin', headers: {} };
  if (init.method !== 'GET') {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body || {});
  }
  const res = await fetch(path, init).catch(() => {
    throw new Error('网络连接失败，请确认服务仍在运行，然后重试。');
  });
  let data = {};
  try { data = await res.json(); } catch (err) { data = {}; }
  if (res.status === 401 && !opts.quiet) {
    state.user = null;
    location.hash = '#/login';
    throw new Error(data.error || '请先登录');
  }
  if (!res.ok) {
    const error = new Error(data.error || ('请求失败（' + res.status + '）'));
    error.status = res.status;
    error.code = data.code || '';
    throw error;
  }
  return data;
}

const STATUS_LABEL = { pending: '待确认错题', confirmed: '已确认', returned: '已退回' };

async function ensureMeta() {
  if (state.meta) return state.meta;
  try { state.meta = await api('/api/meta', { quiet: true }); } catch (err) { state.meta = null; }
  updateAiBadge();
  return state.meta;
}

function statusChip(status) {
  return h('span', { class: 'status ' + status }, STATUS_LABEL[status] || status);
}

function studentText(name, note) {
  return note ? name + '（' + note + '）' : name;
}

function studentLabel(student) {
  return studentText(student.name, student.note);
}

function aiMode() {
  return state.meta ? state.meta.ai_mode : 'mock';
}

function aiModelName() {
  return state.meta && state.meta.ai_model ? state.meta.ai_model : '';
}

function aiModeBanner() {
  if (aiMode() === 'real') {
    return h('div', { class: 'banner real' },
      h('b', {}, '真实 AI 分析（' + aiModelName() + '）：'),
      '提交后会把错题照片、题目文字与解题过程发送给阿里云百炼，不发送学生姓名和班级名。分析只是建议，必须由你对照原图核对。');
  }
  return h('div', { class: 'banner mock' },
    h('b', {}, '模拟分析：'), '系统不会识别图片内容，也不会把照片发给任何外部服务；分析草稿仅用于演示核对流程。');
}

function updateAiBadge() {
  const badge = document.getElementById('ai-badge');
  if (!badge) return;
  // 未登录时读不到 /api/meta，不猜模式，避免把真实模式标成模拟。
  if (!state.user || !state.meta) {
    badge.style.display = 'none';
    return;
  }
  badge.style.display = '';
  const real = aiMode() === 'real';
  badge.className = real ? 'badge-real' : 'badge-mock';
  badge.textContent = real ? '真实 AI 分析' : '模拟分析模式';
  badge.title = real
    ? '每张照片发送前都需要你勾选同意（模型：' + aiModelName() + '）'
    : '未配置真实 AI，照片不会发送到外部';
}

function fmtDate(iso) {
  if (!iso) return '—';
  return String(iso).slice(0, 10);
}

// 给人看的保存时间：YYYY-MM-DD HH:MM（不把 ISO 串原样丢给老师）。
function fmtDateTime(iso) {
  if (!iso) return '—';
  const text = String(iso);
  const when = new Date(text);
  if (Number.isNaN(when.getTime())) return text.replace('T', ' ').slice(0, 16);
  const pad = (n) => String(n).padStart(2, '0');
  return when.getFullYear() + '-' + pad(when.getMonth() + 1) + '-' + pad(when.getDate())
    + ' ' + pad(when.getHours()) + ':' + pad(when.getMinutes());
}

// 给老师看的保存时间：2026-11-24 14:46，而不是原始 ISO 串（含 T 和时区偏移）。
function fmtDateTime(iso) {
  const text = String(iso || '');
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  if (match) return match[1] + ' ' + match[2];
  return text || '—';
}

function today() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
}

/* ------------------------------------------------------------------ */
/* 弹窗                                                                */
/* ------------------------------------------------------------------ */
function dialogShell(title, bodyNodes, footNodes) {
  const dlg = h('dialog', {},
    h('div', { class: 'dlg-head' }, title),
    h('div', { class: 'dlg-body' }, bodyNodes),
    h('div', { class: 'dlg-foot' }, footNodes),
  );
  document.body.appendChild(dlg);
  dlg.showModal();
  dlg.addEventListener('close', () => dlg.remove());
  return dlg;
}

function formDialog(options) {
  return new Promise((resolve) => {
    const values = {};
    const fields = options.fields.map((field) => {
      values[field.name] = field.value !== undefined ? field.value : (field.type === 'textarea' ? '' : '');
      let control;
      if (field.type === 'select') {
        control = h('select', { name: field.name },
          (field.options || []).map((opt) => h('option', { value: opt.value, selected: opt.value === field.value }, opt.label)),
        );
        control.addEventListener('change', () => { values[field.name] = control.value; });
      } else if (field.type === 'textarea') {
        control = h('textarea', { name: field.name, rows: field.rows || 3, placeholder: field.placeholder || '' });
        control.value = field.value || '';
        control.addEventListener('input', () => { values[field.name] = control.value; });
      } else {
        control = h('input', { type: field.type || 'text', name: field.name, placeholder: field.placeholder || '', value: field.value || '' });
        control.addEventListener('input', () => { values[field.name] = control.value; });
      }
      return h('label', { class: 'field', style: 'margin-bottom:12px' },
        h('span', { class: 'lab' }, field.label),
        control,
        field.hint ? h('div', { class: 'ai-note' }, field.hint) : null,
      );
    });

    const errorLine = h('div', { class: 'small', style: 'color:var(--danger)' }, '');

    const submit = async () => {
      errorLine.textContent = '';
      for (const field of options.fields) {
        if (field.required && !String(values[field.name] || '').trim()) {
          errorLine.textContent = '请填写' + field.label;
          return;
        }
      }
      if (options.onSubmit) {
        try {
          const result = await options.onSubmit(values);
          dlg.close();
          resolve(result === undefined ? values : result);
        } catch (err) {
          errorLine.textContent = err.message;
        }
      } else {
        dlg.close();
        resolve(values);
      }
    };

    const dlg = dialogShell(
      options.title,
      [...fields, errorLine],
      [
        h('button', { class: 'btn ghost', onclick: () => { dlg.close(); resolve(null); } }, '取消'),
        h('button', { class: 'btn ' + (options.danger ? 'danger' : 'primary'), onclick: submit }, options.confirmText || '确定'),
      ],
    );
  });
}

function confirmDialog(options) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => { if (!settled) { settled = true; resolve(value); } };
    const dlg = dialogShell(
      options.title,
      [h('div', { class: 'stack' }, options.message)],
      [
        h('button', { class: 'btn ghost', onclick: () => { finish(false); dlg.close(); } }, '取消'),
        h('button', { class: 'btn ' + (options.danger ? 'danger' : 'primary'), onclick: () => { finish(true); dlg.close(); } }, options.confirmText || '确定'),
      ],
    );
    dlg.addEventListener('close', () => finish(false));
  });
}

/* ------------------------------------------------------------------ */
/* 登录                                                                */
/* ------------------------------------------------------------------ */
function renderLogin() {
  document.getElementById('topbar').hidden = true;
  const errorLine = h('div', { class: 'small error-line' }, '');
  const username = h('input', { type: 'text', value: 'demo', autocomplete: 'username' });
  const password = h('input', { type: 'password', autocomplete: 'current-password' });
  const demoHint = h('p', { class: 'tiny muted' }, '演示账号：demo（密码见 README 的「演示账号」一节，可在设置里改密码）');
  const submit = async (event) => {
    event.preventDefault();
    errorLine.textContent = '';
    try {
      const data = await api('/api/auth/login', { method: 'POST', body: { username: username.value, password: password.value }, quiet: true });
      state.user = data.user;
      await ensureMeta();
      location.hash = '#/classes';
      await renderRoute();
    } catch (err) {
      errorLine.textContent = err.message;
    }
  };

  $app.replaceChildren(
    h('div', { class: 'login-card stack' },
      h('h2', {}, '数学错题学情分析'),
      h('p', { class: 'small muted' }, '教师端 · 仅老师使用，学生无需登录。'),
      h('div', { class: 'banner mock' },
        '分析内容可能是本地模拟，也可能是真实 AI 建议：登录后页面顶部会标明当前模式；真实模式发送照片前需要你逐张确认。'),
      h('form', { onsubmit: submit, class: 'stack' },
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '账号'), username),
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '密码'), password),
        errorLine,
        h('button', { class: 'btn primary', type: 'submit', style: 'width:100%' }, '登录'),
      ),
      h('p', { class: 'tiny muted' }, '还没有账号？', h('a', { href: '#/register' }, '注册老师账号')),
      demoHint,
    ),
  );
  api('/api/info', { quiet: true })
    .then((info) => {
      if (info && !info.demo_enabled) {
        demoHint.textContent = '首次使用请点上面的「注册老师账号」；账号只保存在这台电脑上。';
        if (username.value === 'demo') username.value = '';
      }
    })
    .catch(() => {});
}

function renderRegister() {
  document.getElementById('topbar').hidden = true;
  const errorLine = h('div', { class: 'small error-line' }, '');
  const username = h('input', { type: 'text', autocomplete: 'username', placeholder: '用于登录，不能与已有账号重复' });
  const password = h('input', { type: 'password', autocomplete: 'new-password', placeholder: '至少 8 位' });
  const confirm = h('input', { type: 'password', autocomplete: 'new-password', placeholder: '再输入一次' });

  const submit = async (event) => {
    event.preventDefault();
    errorLine.textContent = '';
    const name = username.value.trim();
    if (!name) { errorLine.textContent = '请输入用户名'; username.focus(); return; }
    if (password.value.length < 8) { errorLine.textContent = '密码至少 8 位'; password.focus(); return; }
    if (password.value !== confirm.value) { errorLine.textContent = '两次输入的密码不一致'; confirm.focus(); return; }
    try {
      const data = await api('/api/auth/register', {
        method: 'POST', body: { username: name, password: password.value }, quiet: true,
      });
      state.user = data.user;
      state.meta = null;
      await ensureMeta();
      toast('注册成功，已登录');
      location.hash = '#/classes';
      await renderRoute();
    } catch (err) {
      errorLine.textContent = err.message;
    }
  };

  $app.replaceChildren(
    h('div', { class: 'login-card stack' },
      h('h2', {}, '注册老师账号'),
      h('p', { class: 'small muted' }, '第一版只需用户名和密码；暂不支持短信、邮箱或第三方登录。'),
      h('form', { onsubmit: submit, class: 'stack' },
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '用户名'), username),
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '密码'), password),
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '确认密码'), confirm),
        errorLine,
        h('button', { class: 'btn primary', type: 'submit', style: 'width:100%' }, '注册并登录'),
      ),
      h('p', { class: 'tiny muted' }, '已有账号？', h('a', { href: '#/login' }, '返回登录')),
      h('p', { class: 'tiny muted' }, '注册后是独立账号，不会看到演示账号或别的老师的数据。'),
    ),
  );
}

/* ------------------------------------------------------------------ */
/* 班级列表                                                            */
/* ------------------------------------------------------------------ */
async function renderClasses() {
  const data = await api('/api/classes');
  const grid = h('div', { class: 'class-list' },
    data.items.map((item) => h('div', { class: 'class-card' },
      h('h3', {}, h('a', { href: '#/class/' + item.id + '/check' }, item.name)),
      h('div', { class: 'small muted' }, [item.grade, item.note].filter(Boolean).join(' · ') || '　'),
      h('div', { class: 'stat-inline' },
        h('span', {}, h('b', {}, item.student_count), '学生'),
        h('span', {}, h('b', { style: 'color:var(--warn)' }, item.pending), '待确认错题'),
        h('span', {}, h('b', { style: 'color:var(--ok)' }, item.confirmed), '已确认错题'),
      ),
      h('div', { class: 'row', style: 'margin-top:12px' },
        // A1：进班级直接落在「作答核对」，不绕学生名单。
        h('a', { class: 'btn sm primary', href: '#/class/' + item.id + '/check' }, '进入班级'),
        h('a', { class: 'btn sm', href: '#/upload?class=' + item.id }, '上传错题'),
      ),
    )),
  );

  $app.replaceChildren(
    h('div', { class: 'row between', style: 'margin-bottom:14px' },
      h('h2', {}, '我的班级'),
      // A1：新建班级是次级入口，不与主操作抢注意力。
      h('button', { class: 'btn sm', onclick: createClass }, '＋ 新建班级'),
    ),
    aiModeBanner(),
    data.items.length ? grid : h('div', { class: 'card muted' }, '还没有班级。点击右上角「新建班级」开始。'),
  );

  async function createClass() {
    const result = await formDialog({
      title: '新建班级',
      fields: [
        { name: 'name', label: '班级名称', required: true, placeholder: '例如：初二(3)班' },
        { name: 'grade', label: '年级（可选）', placeholder: '例如：八年级' },
        { name: 'note', label: '备注（可选）' },
      ],
      confirmText: '创建',
      onSubmit: (values) => api('/api/classes', { method: 'POST', body: values }),
    });
    if (result) {
      toast('班级已创建');
      await renderClasses();
    }
  }
}

/* ------------------------------------------------------------------ */
/* 班级页                                                              */
/* ------------------------------------------------------------------ */
async function renderClass(classId, tab, sub, query) {
  const data = await api('/api/classes/' + classId);
  state.currentClass = data;

  const tabs = [
    // A1：作答核对放在班级页第一个 tab，进班级默认落在这里。
    ['check', '作答核对'],
    ['overview', '概况'],
    ['students', '学生'],
    ['mistakes', '错题'],
    ['stats', '统计'],
    ['advice', '教学建议'],
  ];

  const body = h('div', {}, h('div', { class: 'muted' }, '加载中…'));
  const page = h('div', {},
    h('div', { class: 'row between', style: 'margin-bottom:10px' },
      h('div', {},
        h('div', { class: 'small' }, h('a', { href: '#/classes' }, '← 我的班级')),
        h('h2', {}, data.name, ' ', h('span', { class: 'small muted' }, data.grade || '')),
      ),
      h('div', { class: 'row' },
        h('button', { class: 'btn sm', onclick: () => editClass(data) }, '编辑班级'),
        // A6：作答核对页只允许一个 .btn.primary，上传错题在这里降为次级按钮。
        h('a', { class: tab === 'check' ? 'btn sm' : 'btn sm primary', href: '#/upload?class=' + data.id }, '＋ 上传错题'),
        h('button', { class: 'btn sm danger', onclick: () => purgeClass(data) }, '清空数据'),
      ),
    ),
    h('div', { class: 'tabs' }, tabs.map(([key, label]) =>
      h('a', { href: '#/class/' + data.id + '/' + key, class: key === tab ? 'active' : '' }, label))),
    body,
  );
  $app.replaceChildren(page);

  const renderers = {
    check: () => renderCheckTab(data, sub, query || {}),
    overview: renderOverview,
    students: renderStudents,
    mistakes: renderMistakesTab,
    stats: renderStatsTab,
    advice: renderAdviceTab,
  };
  const renderer = renderers[tab] || renderOverview;
  try {
    body.replaceChildren(await renderer(data));
  } catch (err) {
    body.replaceChildren(h('div', { class: 'banner danger' }, err.message));
  }

  async function editClass(row) {
    const result = await formDialog({
      title: '编辑班级',
      fields: [
        { name: 'name', label: '班级名称', required: true, value: row.name },
        { name: 'grade', label: '年级', value: row.grade },
        { name: 'note', label: '备注', value: row.note },
      ],
      onSubmit: (values) => api('/api/classes/' + row.id, { method: 'PATCH', body: values }),
    });
    if (result) { toast('已保存'); await renderRoute(); }
  }

  async function purgeClass(row) {
    const result = await formDialog({
      title: '清空班级数据',
      danger: true,
      fields: [
        { name: 'confirm_name', label: '输入班级名称「' + row.name + '」确认清空', required: true },
      ],
      confirmText: '确认清空',
      onSubmit: (values) => api('/api/classes/' + row.id + '/purge', { method: 'POST', body: values }),
    });
    if (result) {
      toast('已清空：学生 ' + result.deleted_students + ' 名，错题 ' + result.deleted_mistakes + ' 条，照片 ' + result.deleted_files + ' 张');
      await renderRoute();
    }
  }
}

async function renderOverview(data) {
  const pending = await api('/api/mistakes?class_id=' + data.id + '&status=pending');
  const items = [
    h('div', { class: 'kpi' },
      kpiBox(data.student_count, '学生'),
      kpiBox(data.pending, '待确认错题'),
      kpiBox(data.confirmed, '已确认错题'),
      kpiBox(data.returned, '已退回错题'),
    ),
  ];

  items.push(h('div', { class: 'card', style: 'margin-top:16px' },
    h('div', { class: 'row between' },
      h('h3', {}, '待确认错题'),
      h('a', { class: 'small', href: '#/class/' + data.id + '/mistakes?status=pending' }, '查看全部'),
    ),
    pending.items.length
      ? h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '学生'), h('th', {}, '题目'), h('th', { class: 'hide-sm' }, '来源'), h('th', {}, '录入'))),
        h('tbody', {}, pending.items.slice(0, 6).map((item) => h('tr', {
          class: 'clickable',
          onclick: () => { location.hash = '#/mistake/' + item.id; },
        },
        h('td', {}, studentText(item.student_name, item.student_note)),
        h('td', {}, item.question_text || h('span', { class: 'muted' }, '（无题目文本）'),
          item.insufficient ? h('span', { class: 'chip warn', style: 'margin-left:6px' }, '信息不足') : null),
        h('td', { class: 'hide-sm' }, item.source_type),
        h('td', { class: 'small muted' }, fmtDate(item.occurred_on)),
        ))),
      )
      : h('p', { class: 'muted small' }, '没有待确认错题。'),
  ));

  items.push(h('div', { class: 'card' },
    h('div', { class: 'row between' }, h('h3', {}, '学生名单'),
      h('a', { class: 'small', href: '#/class/' + data.id + '/students' }, '管理')),
    data.students.length
      ? h('div', { class: 'row', style: 'margin-top:8px' },
        data.students.map((s) => h('a', { class: 'chip', href: '#/student/' + s.id }, studentLabel(s))))
      : h('p', { class: 'muted small' }, '还没有学生，先到「学生」页添加。'),
  ));

  return h('div', {}, items);
}

function kpiBox(number, label) {
  return h('div', { class: 'box' }, h('div', { class: 'n' }, number), h('div', { class: 'l' }, label));
}

async function renderStudents(data) {
  const rows = await Promise.all(data.students.map(async (student) => {
    const stats = await api('/api/students/' + student.id + '/stats');
    return { student, stats };
  }));

  return h('div', { class: 'card' },
    h('div', { class: 'row between' },
      h('h3', {}, '学生名单（' + data.students.length + '）'),
      h('button', { class: 'btn sm primary', onclick: addStudent }, '＋ 添加学生'),
    ),
    h('p', { class: 'small muted', style: 'margin-top:6px' },
      '同名学生可以用右侧的“区分备注”分辨，例如“三组”“五组”，不需要学号。'),
    h('table', {},
      h('thead', {}, h('tr', {},
        h('th', {}, '姓名'), h('th', {}, '区分备注'), h('th', {}, '已确认'),
        h('th', {}, '待确认错题'), h('th', {}, '已退回'), h('th', {}, '操作'))),
      h('tbody', {}, rows.map(({ student, stats }) => h('tr', {},
        h('td', {}, h('a', { href: '#/student/' + student.id }, student.name)),
        h('td', { class: 'muted' }, student.note || '—'),
        h('td', { class: 'num' }, stats.summary.confirmed),
        h('td', { class: 'num' }, stats.summary.pending),
        h('td', { class: 'num' }, stats.summary.returned),
        h('td', {},
          h('button', { class: 'btn sm ghost', onclick: () => editStudent(student) }, '改名'),
          h('button', { class: 'btn sm ghost danger', onclick: () => removeStudent(student) }, '删除'),
        ),
      ))),
    ),
  );

  async function addStudent() {
    const result = await formDialog({
      title: '添加学生',
      fields: [
        { name: 'name', label: '姓名', required: true },
        { name: 'note', label: '区分备注（可选）', placeholder: '同名学生可填“三组”“五组”' },
      ],
      onSubmit: (values) => api('/api/classes/' + data.id + '/students', { method: 'POST', body: values }),
    });
    if (result) { toast('已添加'); await renderRoute(); }
  }

  async function editStudent(student) {
    const result = await formDialog({
      title: '修改学生',
      fields: [
        { name: 'name', label: '姓名', required: true, value: student.name },
        { name: 'note', label: '区分备注', value: student.note },
      ],
      onSubmit: (values) => api('/api/students/' + student.id, { method: 'PATCH', body: values }),
    });
    if (result) { toast('已保存'); await renderRoute(); }
  }

  async function removeStudent(student) {
    const ok = await confirmDialog({
      title: '删除学生',
      danger: true,
      message: '删除「' + studentLabel(student) + '」会同时删除该学生的所有错题与照片。确定继续？',
      confirmText: '删除',
    });
    if (!ok) return;
    try {
      const result = await api('/api/students/' + student.id, { method: 'DELETE' });
      toast('已删除学生及其 ' + result.deleted_mistakes + ' 条错题');
      await renderRoute();
    } catch (err) { toast(err.message, true); }
  }
}

async function renderMistakesTab(data) {
  const filters = state.ui.mistakeFilters;
  const params = new URLSearchParams({ class_id: data.id });
  ['status', 'student_id', 'kp', 'error_type', 'from', 'to'].forEach((key) => {
    if (filters[key]) params.set(key, filters[key]);
  });
  const list = await api('/api/mistakes?' + params.toString());
  const options = await api('/api/classes/' + data.id + '/stats?from=&to=');

  const statusTabs = [['', '全部'], ['pending', '待确认错题'], ['confirmed', '已确认'], ['returned', '已退回']];
  const total = Object.values(list.counts).reduce((a, b) => a + b, 0);
  const tabCount = (key) => (key ? (list.counts[key] || 0) : total);

  const filterBar = h('div', { class: 'row', style: 'margin:10px 0' },
    selectBox('学生', [['', '全部学生']].concat(data.students.map((s) => [String(s.id), studentLabel(s)])), filters.student_id || '', (value) => {
      filters.student_id = value; renderRoute();
    }),
    selectBox('知识点', [['', '全部知识点']].concat(options.options.knowledge_points.map((k) => [k, k])), filters.kp || '', (value) => {
      filters.kp = value; renderRoute();
    }),
    selectBox('错误类型', [['', '全部类型']].concat(options.options.error_types.map((k) => [k, k])), filters.error_type || '', (value) => {
      filters.error_type = value; renderRoute();
    }),
    h('label', { class: 'small muted' }, '从 ',
      h('input', { type: 'date', value: filters.from || '', onchange: (event) => { filters.from = event.target.value; renderRoute(); } })),
    h('label', { class: 'small muted' }, '到 ',
      h('input', { type: 'date', value: filters.to || '', onchange: (event) => { filters.to = event.target.value; renderRoute(); } })),
    h('button', { class: 'btn sm ghost', onclick: () => { state.ui.mistakeFilters = {}; renderRoute(); } }, '清除筛选'),
  );

  return h('div', {},
    h('div', { class: 'tabs' }, statusTabs.map(([key, label]) =>
      h('a', {
        href: '#/class/' + data.id + '/mistakes',
        class: (filters.status || '') === key ? 'active' : '',
        onclick: (event) => { event.preventDefault(); filters.status = key; renderRoute(); },
      }, label + '（' + tabCount(key) + '）'))),
    filterBar,
    h('div', { class: 'card' },
      list.items.length
        ? h('table', {},
          h('thead', {}, h('tr', {},
            h('th', {}, '学生'), h('th', {}, '题目 / 知识点'), h('th', {}, '错误类型'),
            h('th', { class: 'hide-sm' }, '来源'), h('th', { class: 'hide-sm' }, '日期'), h('th', {}, '状态'))),
          h('tbody', {}, list.items.map((item) => h('tr', {
            class: 'clickable',
            onclick: () => { location.hash = '#/mistake/' + item.id; },
          },
          h('td', {}, studentText(item.student_name, item.student_note)),
          h('td', {},
            h('div', {}, item.question_text || h('span', { class: 'muted' }, '（无题目文本）')),
            h('div', { class: 'row', style: 'gap:4px;margin-top:4px' },
              item.knowledge_points.map((kp) => h('span', { class: 'chip' }, kp)),
              item.insufficient ? h('span', { class: 'chip warn' }, '信息不足') : null),
          ),
          h('td', {}, item.error_type || h('span', { class: 'muted' }, '—')),
          h('td', { class: 'hide-sm small' }, item.source_type + (item.source_detail ? ' · ' + item.source_detail : '')),
          h('td', { class: 'hide-sm small muted' }, fmtDate(item.occurred_on)),
          h('td', {}, statusChip(item.status)),
          ))),
        )
        : h('p', { class: 'muted' }, '没有符合条件的错题。'),
    ),
  );
}

function selectBox(label, options, value, onChange) {
  const select = h('select', { style: 'width:auto;min-width:120px' },
    options.map(([key, text]) => h('option', { value: key, selected: key === value }, text)));
  select.addEventListener('change', () => onChange(select.value));
  return h('label', { class: 'small muted row', style: 'gap:6px' }, label, select);
}

/* ------------------------------------------------------------------ */
/* 统计                                                                */
/* ------------------------------------------------------------------ */
async function renderStatsTab(data) {
  const filters = state.ui.statsFilters;
  const params = new URLSearchParams();
  ['from', 'to', 'kp', 'error_type', 'student_id'].forEach((key) => { if (filters[key]) params.set(key, filters[key]); });
  const stats = await api('/api/classes/' + data.id + '/stats?' + params.toString());

  const filterBar = h('div', { class: 'row', style: 'margin-bottom:12px' },
    selectBox('学生', [['', '全班']].concat(stats.students.map((s) => [String(s.id), studentLabel(s)])), filters.student_id || '', (value) => {
      filters.student_id = value; renderRoute();
    }),
    selectBox('知识点', [['', '全部']].concat(stats.options.knowledge_points.map((k) => [k, k])), filters.kp || '', (value) => {
      filters.kp = value; renderRoute();
    }),
    selectBox('错误类型', [['', '全部']].concat(stats.options.error_types.map((k) => [k, k])), filters.error_type || '', (value) => {
      filters.error_type = value; renderRoute();
    }),
    h('label', { class: 'small muted row', style: 'gap:6px' }, '从',
      h('input', { type: 'date', value: filters.from || '', style: 'width:150px', onchange: (event) => { filters.from = event.target.value; renderRoute(); } })),
    h('label', { class: 'small muted row', style: 'gap:6px' }, '到',
      h('input', { type: 'date', value: filters.to || '', style: 'width:150px', onchange: (event) => { filters.to = event.target.value; renderRoute(); } })),
    h('button', { class: 'btn sm ghost', onclick: () => { state.ui.statsFilters = {}; renderRoute(); } }, '清除筛选'),
  );

  const drill = h('div', {});
  const openDrill = async (label, ids) => {
    let result;
    try {
      result = await api('/api/mistake-list?ids=' + ids.join(','));
    } catch (err) {
      toast(err.message, true);
      return;
    }
    drill.replaceChildren(h('div', { class: 'card', style: 'margin-top:12px' },
      h('div', { class: 'row between' },
        h('h3', {}, '「' + label + '」对应的已确认错题（' + result.items.length + ' 条）'),
        h('button', { class: 'btn sm ghost', onclick: () => drill.replaceChildren() }, '收起')),
      mistakeBriefTable(result.items),
    ));
  };

  const maxKp = Math.max(1, ...stats.knowledge_points.map((k) => k.mistakes));
  const maxErr = Math.max(1, ...stats.error_types.map((k) => k.mistakes));

  const body = [
    filterBar,
    h('div', { class: 'kpi' },
      kpiBox(stats.summary.confirmed, '已确认错题（计入统计）'),
      kpiBox(stats.summary.students, '涉及学生'),
      kpiBox(stats.summary.pending, '待确认错题（不计入）'),
      kpiBox(stats.summary.returned, '已退回（不计入）'),
    ),
    stats.notes.length ? h('div', { class: 'banner info', style: 'margin-top:14px' },
      h('div', {}, stats.notes.map((note) => h('div', {}, '· ' + note)))) : null,
    h('div', { class: 'card', style: 'margin-top:14px' },
      h('h3', {}, '常见知识点'),
      h('p', { class: 'small muted' }, '同一个知识点同时给出「错题条数」和「涉及学生人数」，两者含义不同。'),
      stats.knowledge_points.length
        ? h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, '知识点'), h('th', {}, '错题条数'), h('th', {}, '涉及学生'), h('th', { class: 'hide-sm' }, '分布'), h('th', {}, ''))),
          h('tbody', {}, stats.knowledge_points.map((item) => h('tr', { class: 'clickable', onclick: () => openDrill(item.name, item.mistake_ids) },
            h('td', {}, item.name),
            h('td', { class: 'num' }, item.mistakes),
            h('td', { class: 'num' }, item.student_count),
            h('td', { class: 'hide-sm', style: 'width:180px' },
              h('div', { class: 'bar' }, h('i', { style: 'width:' + Math.round(item.mistakes / maxKp * 100) + '%' }))),
            h('td', { class: 'small muted' }, '查看错题 →'),
          ))),
        )
        : h('p', { class: 'muted small' }, '暂无可统计的知识点。'),
    ),
    h('div', { class: 'card' },
      h('h3', {}, '常见错误类型'),
      stats.error_types.length
        ? h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, '错误类型'), h('th', {}, '错题条数'), h('th', {}, '涉及学生'), h('th', { class: 'hide-sm' }, '分布'), h('th', {}, ''))),
          h('tbody', {}, stats.error_types.map((item) => h('tr', { class: 'clickable', onclick: () => openDrill(item.name, item.mistake_ids) },
            h('td', {}, item.name),
            h('td', { class: 'num' }, item.mistakes),
            h('td', { class: 'num' }, item.student_count),
            h('td', { class: 'hide-sm', style: 'width:180px' },
              h('div', { class: 'bar' }, h('i', { style: 'width:' + Math.round(item.mistakes / maxErr * 100) + '%' }))),
            h('td', { class: 'small muted' }, '查看错题 →'),
          ))),
        )
        : h('p', { class: 'muted small' }, '暂无可统计的错误类型。'),
    ),
    h('div', { class: 'card' },
      h('h3', {}, '统计依据'),
      h('p', { class: 'small' }, stats.basis),
      h('p', { class: 'tiny muted' }, stats.rule),
    ),
    drill,
  ];
  return h('div', {}, body);
}

function mistakeBriefTable(items) {
  return h('table', {},
    h('thead', {}, h('tr', {},
      h('th', {}, '学生'), h('th', {}, '题目'), h('th', {}, '知识点'), h('th', {}, '错误类型'), h('th', {}, '日期'))),
    h('tbody', {}, items.map((item) => h('tr', {
      class: 'clickable',
      onclick: () => { location.hash = '#/mistake/' + item.id; },
    },
    h('td', {}, studentText(item.student_name, item.student_note)),
    h('td', {}, item.question_text || h('span', { class: 'muted' }, '（无题目文本）')),
    h('td', {}, item.knowledge_points.join('、') || '—'),
    h('td', {}, item.error_type || '—'),
    h('td', { class: 'small muted' }, fmtDate(item.occurred_on)),
    ))),
  );
}

/* ------------------------------------------------------------------ */
/* 教学建议                                                            */
/* ------------------------------------------------------------------ */
async function renderAdviceTab(data) {
  const advice = await api('/api/classes/' + data.id + '/advice');
  const refreshButton = h('button', {
    class: 'btn sm',
    onclick: async () => {
      await api('/api/classes/' + data.id + '/advice/refresh', { method: 'POST' });
      toast('建议已按最新已确认错题重新生成');
      renderRoute();
    },
  }, '按最新统计刷新');

  return h('div', {},
    h('div', { class: 'banner mock' },
      '建议由系统根据「已确认」错题生成，属于参考意见；不会自动布置作业，也不替代老师的教学判断。'),
    advice.class_advice
      ? adviceCard(advice.class_advice, '班级教学建议（参考意见）', refreshButton)
      : h('div', { class: 'card muted' }, '暂无建议。'),
    h('div', { class: 'card' },
      h('h3', {}, '学生学习建议'),
      h('p', { class: 'small muted' }, '只有 1 条已确认错题时，系统会标注“证据不足”，不给出具体结论。'),
      advice.student_advices.length
        ? h('div', {}, advice.student_advices.map((item) => {
          const student = data.students.find((s) => s.id === item.student_id);
          return adviceCard(item, (student ? studentLabel(student) : '学生') + ' · 学习建议', null);
        }))
        : h('p', { class: 'muted small' }, '还没有学生建议。'),
    ),
  );
}

function adviceCard(item, title, extraButton) {
  const textarea = h('textarea', { rows: 6 });
  textarea.value = item.content;
  const status = h('div', { class: 'row', style: 'margin-bottom:8px' },
    h('h3', {}, title),
    item.teacher_edited ? h('span', { class: 'chip edited' }, '老师已修改') : h('span', { class: 'chip mock' }, '系统生成'),
    h('span', { class: 'chip' }, '依据 ' + item.evidence_count + ' 条已确认错题'),
  );
  const saveButton = h('button', {
    class: 'btn sm primary',
    onclick: async () => {
      try {
        await api('/api/advice/' + item.id, { method: 'PATCH', body: { content: textarea.value } });
        toast('建议已保存');
        renderRoute();
      } catch (err) { toast(err.message, true); }
    },
  }, '保存修改');

  const evidenceButton = h('button', {
    class: 'btn sm ghost',
    onclick: async () => {
      const ids = (item.evidence && item.evidence.mistake_ids) || [];
      if (!ids.length) { toast('该建议目前没有错题依据', true); return; }
      const result = await api('/api/mistake-list?ids=' + ids.join(','));
      const dlg = dialogShell('建议依据（' + result.items.length + ' 条已确认错题）',
        [mistakeBriefTable(result.items)],
        [h('button', { class: 'btn primary', onclick: () => dlg.close() }, '知道了')]);
    },
  }, '查看依据');

  return h('div', { class: 'card' },
    status,
    textarea,
    h('div', { class: 'row', style: 'margin-top:8px' }, saveButton, evidenceButton, extraButton),
  );
}

/* ------------------------------------------------------------------ */
/* 学生个人页                                                          */
/* ------------------------------------------------------------------ */
async function renderStudent(studentId) {
  const stats = await api('/api/students/' + studentId + '/stats');
  const student = stats.student;
  const list = await api('/api/mistakes?class_id=' + stats.class.id + '&student_id=' + studentId + '&status=confirmed');

  $app.replaceChildren(h('div', {},
    h('div', { class: 'small' }, h('a', { href: '#/class/' + stats.class.id + '/students' }, '← 返回 ' + stats.class.name)),
    h('h2', { style: 'margin:6px 0 14px' }, studentLabel(student), ' ', h('span', { class: 'small muted' }, '个人错题情况')),
    h('div', { class: 'kpi' },
      kpiBox(stats.summary.confirmed, '已确认错题'),
      kpiBox(stats.knowledge_points.length, '涉及知识点'),
      kpiBox(stats.summary.pending, '待确认错题'),
      kpiBox(stats.summary.returned, '已退回'),
    ),
    stats.notes.length ? h('div', { class: 'banner info', style: 'margin-top:14px' },
      stats.notes.map((note) => h('div', {}, '· ' + note))) : null,
    h('div', { class: 'card', style: 'margin-top:14px' },
      h('h3', {}, '常见知识点'),
      stats.knowledge_points.length
        ? h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, '知识点'), h('th', {}, '错题条数'), h('th', {}, '涉及学生'))),
          h('tbody', {}, stats.knowledge_points.map((item) => h('tr', {},
            h('td', {}, item.name), h('td', { class: 'num' }, item.mistakes), h('td', { class: 'num' }, item.student_count)))),
        )
        : h('p', { class: 'muted small' }, '暂无可统计的知识点。'),
    ),
    h('div', { class: 'card' },
      h('h3', {}, '常见错误类型'),
      stats.error_types.length
        ? h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, '错误类型'), h('th', {}, '错题条数'), h('th', {}, '涉及学生'))),
          h('tbody', {}, stats.error_types.map((item) => h('tr', {},
            h('td', {}, item.name), h('td', { class: 'num' }, item.mistakes), h('td', { class: 'num' }, item.student_count)))),
        )
        : h('p', { class: 'muted small' }, '暂无可统计的错误类型。'),
    ),
    h('div', { class: 'card' },
      h('h3', {}, '已确认错题（' + list.items.length + '）'),
      list.items.length ? mistakeBriefTable(list.items) : h('p', { class: 'muted small' }, '暂无已确认错题。'),
    ),
    h('div', { class: 'card' }, h('h3', {}, '统计依据'),
      h('p', { class: 'small' }, stats.basis),
      h('p', { class: 'tiny muted' }, stats.rule)),
  ));
}

/* ------------------------------------------------------------------ */
/* 上传错题                                                            */
/* ------------------------------------------------------------------ */
async function renderUpload(query) {
  const classes = await api('/api/classes');
  if (!classes.items.length) {
    $app.replaceChildren(h('div', { class: 'card' }, '还没有班级，请先创建班级和学生。'));
    return;
  }
  let currentClass = classes.items.find((c) => String(c.id) === String(query.class || '')) || classes.items[0];
  let classDetail = await api('/api/classes/' + currentClass.id);
  let selectedStudentId = query.student ? Number(query.student) : (classDetail.students[0] ? classDetail.students[0].id : null);
  let filePayload = null;
  let previewUrl = null;

  const classSelect = h('select', {}, classes.items.map((c) => h('option', { value: c.id, selected: c.id === currentClass.id }, c.name)));
  const studentSelect = h('select', {});
  const sourceType = h('select', {}, ['作业', '考试', '练习', '其他'].map((t) => h('option', { value: t }, t)));
  const sourceDetail = h('input', { type: 'text', placeholder: '例如：课本 P42 第 3 题' });
  const occurred = h('input', { type: 'date', value: today() });
  const question = h('textarea', { rows: 3, placeholder: '可选：把题目抄一遍或输入文字，便于分析和核对' });
  const solution = h('textarea', { rows: 3, placeholder: '可选：学生解题过程的文字说明（看不清的部分不要补写）' });
  const blurry = h('input', { type: 'checkbox' });
  const consent = h('input', { type: 'checkbox' });
  const isReal = aiMode() === 'real';
  const drop = h('div', { class: 'drop' },
    h('div', { class: 'muted' }, '还没有选择图片'),
    h('div', { class: 'tiny muted' }, '支持手机拍照、手机相册、电脑文件；系统会在本机压缩到最长边 1800 像素后保存'));
  const fileInputCamera = h('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'file-input' });
  const fileInputPick = h('input', { type: 'file', accept: 'image/*', class: 'file-input' });

  function refreshStudents() {
    studentSelect.replaceChildren(...classDetail.students.map((s) =>
      h('option', { value: s.id, selected: s.id === selectedStudentId }, studentLabel(s))));
    if (!classDetail.students.some((s) => s.id === selectedStudentId)) {
      selectedStudentId = classDetail.students[0] ? classDetail.students[0].id : null;
    }
    studentSelect.value = selectedStudentId || '';
  }
  refreshStudents();

  classSelect.addEventListener('change', async () => {
    currentClass = classes.items.find((c) => String(c.id) === classSelect.value) || classes.items[0];
    classDetail = await api('/api/classes/' + currentClass.id);
    selectedStudentId = classDetail.students[0] ? classDetail.students[0].id : null;
    refreshStudents();
  });
  studentSelect.addEventListener('change', () => { selectedStudentId = Number(studentSelect.value); });

  async function handleFile(file) {
    if (!file) return;
    try {
      drop.replaceChildren(h('div', { class: 'muted' }, '正在处理图片…'));
      filePayload = await fileToPayload(file);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      drop.className = 'drop has-file';
      drop.replaceChildren(
        h('img', { src: previewUrl, alt: '待上传的错题照片' }),
        h('div', { class: 'tiny muted', style: 'margin-top:6px' },
          '已准备：' + filePayload.width + '×' + filePayload.height + ' 像素'),
      );
    } catch (err) {
      toast('图片处理失败：' + err.message, true);
      drop.className = 'drop';
      drop.replaceChildren(h('div', { class: 'muted' }, '还没有选择图片'));
    }
  }

  fileInputCamera.addEventListener('change', () => handleFile(fileInputCamera.files[0]));
  fileInputPick.addEventListener('change', () => handleFile(fileInputPick.files[0]));

  const submitButton = h('button', {
    class: 'btn primary',
    style: 'width:100%;padding:14px',
    onclick: async () => {
      if (!filePayload) { toast('请先拍照或选择图片', true); return; }
      if (!selectedStudentId) { toast('请先选择学生', true); return; }
      if (isReal && !consent.checked) {
        toast('请先勾选同意，才会把照片与文字发送给阿里云百炼', true);
        return;
      }
      submitButton.disabled = true;
      submitButton.textContent = isReal ? '正在调用真实 AI…' : '正在分析…';
      try {
        const result = await api('/api/mistakes', {
          method: 'POST',
          body: {
            class_id: currentClass.id,
            student_id: selectedStudentId,
            source_type: sourceType.value,
            source_detail: sourceDetail.value,
            occurred_on: occurred.value,
            question_text: question.value,
            solution_text: solution.value,
            blurry: blurry.checked,
            consent: isReal ? consent.checked : false,
            image: filePayload,
          },
        });
        if (result.ai_error) {
          toast('已保存，但分析失败：请到核对页查看原因并重试', true);
        } else {
          toast('已保存，请核对分析内容');
        }
        location.hash = '#/mistake/' + result.id;
      } catch (err) {
        toast(err.message, true);
        submitButton.disabled = false;
        submitButton.textContent = isReal ? '提交并分析（调用真实 AI）' : '提交并分析';
      }
    },
  }, isReal ? '提交并分析（调用真实 AI）' : '提交并分析');

  $app.replaceChildren(h('div', {},
    h('div', { class: 'small' }, h('a', { href: '#/classes' }, '← 我的班级')),
    h('h2', { style: 'margin:6px 0 14px' }, '上传错题'),
    aiModeBanner(),
    isReal ? h('div', { class: 'banner warn' },
      h('b', {}, '发送前确认：'), '本软件只发送这张错题照片、题目文字和学生解题过程；不发送学生姓名、班级名或账号。',
      '照片本身可能拍到姓名、班级或学校，请先检查。发送后由阿里云百炼按《服务协议》处理数据，本软件的“清空数据”只能删除本机副本。') : null,
    h('div', { class: 'card stack' },
      h('div', { class: 'grid-2' },
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '班级'), classSelect),
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '学生'), studentSelect),
      ),
      h('div', { class: 'upload-actions' },
        h('button', { class: 'btn', onclick: () => fileInputCamera.click() }, '📷 拍照'),
        h('button', { class: 'btn', onclick: () => fileInputPick.click() }, '🖼 从相册 / 电脑选择'),
        fileInputCamera, fileInputPick,
      ),
      drop,
      h('div', { class: 'grid-2' },
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '来源'), sourceType),
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '来源说明（可选）'), sourceDetail),
      ),
      h('div', { class: 'grid-2' },
        h('label', { class: 'field' }, h('span', { class: 'lab' }, '错题日期'), occurred),
        h('label', { class: 'field row', style: 'align-items:center;margin-top:22px' }, blurry, h('span', { class: 'small' }, '图片不清晰或解题过程不完整')),
      ),
      h('label', { class: 'field' }, h('span', { class: 'lab' }, '题目内容（可选）'), question),
      h('label', { class: 'field' }, h('span', { class: 'lab' }, '学生解题过程（可选）'), solution),
      isReal ? h('label', { class: 'field row', style: 'align-items:flex-start;gap:8px' },
        consent,
        h('span', { class: 'small' },
          '我确认把照片、题目文字与解题过程发送给阿里云百炼（不含学生姓名与班级名）。',
          '重试会再次调用服务，可能产生费用。')) : null,
      h('p', { class: 'tiny muted' },
        isReal ? '不填文字也可以分析，但照片必须具备可辨认的题目和解题过程；看不清时模型会提示信息不足。'
          : '不填文字也能提交，但模拟分析只能给出内置示例或「信息不足」提示，不会假装识别了图片。'),
      submitButton,
    ),
  ));
}

function fileToPayload(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const max = 1800;
      const scale = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
      const width = Math.round(img.naturalWidth * scale);
      const height = Math.round(img.naturalHeight * scale);
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      canvas.getContext('2d').drawImage(img, 0, 0, width, height);
      URL.revokeObjectURL(url);
      resolve({ data_url: canvas.toDataURL('image/jpeg', 0.9), width, height });
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('无法读取该图片')); };
    img.src = url;
  });
}

/* ------------------------------------------------------------------ */
/* 错题核对页                                                          */
/* ------------------------------------------------------------------ */
async function renderMistake(mistakeId) {
  await ensureMeta();
  const data = await api('/api/mistakes/' + mistakeId);
  const edited = new Set(data.teacher_edited_fields || []);
  const ai = data.ai || {};

  const draft = {
    student_id: data.student_id,
    question_text: data.question_text,
    solution_text: data.solution_text,
    knowledge_points: data.knowledge_points.join('，'),
    error_type: data.error_type,
    observed_errors: data.observed_errors.join('\n'),
    possible_causes: data.possible_causes.join('\n'),
    advice_text: data.advice_text,
    source_type: data.source_type,
    source_detail: data.source_detail,
    note: data.note,
    teacher_note: data.teacher_note,
    occurred_on: data.occurred_on,
  };
  const dirty = new Set();
  const uncertain = data.possible_causes.length === 1 && data.possible_causes[0] === '暂不确定';

  function markDirty(key) { dirty.add(key); }

  function fieldBadge(key) {
    if (edited.has(key)) return h('span', { class: 'chip edited' }, '老师已修改');
    if (data.ai_error) return h('span', { class: 'chip mock' }, '未生成分析');
    if (data.ai_mode === 'local') return h('span', { class: 'chip mock' }, '本地录入');
    return h('span', { class: 'chip mock' }, data.ai_mode === 'real' ? '真实 AI 建议' : '模拟建议');
  }

  function aiHint(key, formatter) {
    if (!edited.has(key)) return null;
    const value = ai[key];
    if (value === null || value === undefined) return null;
    const text = formatter ? formatter(value) : String(value);
    if (!text) return null;
    return h('div', { class: 'ai-note' }, 'AI 原建议：' + text,
      h('a', {
        href: '#',
        style: 'margin-left:8px',
        onclick: async (event) => {
          event.preventDefault();
          try {
            await api('/api/mistakes/' + data.id + '/revert/' + key, { method: 'POST' });
            toast('已还原为 AI 建议');
            renderRoute();
          } catch (err) { toast(err.message, true); }
        },
      }, '还原'));
  }

  const questionInput = h('textarea', { rows: 3 });
  questionInput.value = draft.question_text;
  questionInput.addEventListener('input', () => { draft.question_text = questionInput.value; markDirty('question_text'); });

  const solutionInput = h('textarea', { rows: 4 });
  solutionInput.value = draft.solution_text;
  solutionInput.addEventListener('input', () => { draft.solution_text = solutionInput.value; markDirty('solution_text'); });

  const kpInput = h('input', { type: 'text', placeholder: '多个知识点用中文逗号分隔' });
  kpInput.value = draft.knowledge_points;
  kpInput.addEventListener('input', () => { draft.knowledge_points = kpInput.value; markDirty('knowledge_points'); });

  const errorSelect = h('select', {},
    [''].concat(state.meta ? state.meta.error_types : []).map((type) =>
      h('option', { value: type, selected: type === draft.error_type }, type || '（未选择）')));
  errorSelect.addEventListener('change', () => { draft.error_type = errorSelect.value; markDirty('error_type'); });

  const observedInput = h('textarea', { rows: 4, placeholder: '每行一条，只写能观察到的现象' });
  observedInput.value = draft.observed_errors;
  observedInput.addEventListener('input', () => { draft.observed_errors = observedInput.value; markDirty('observed_errors'); });

  const causeInput = h('textarea', { rows: 3, placeholder: '每行一条，属于推测' });
  causeInput.value = uncertain ? '' : draft.possible_causes;
  causeInput.disabled = uncertain;
  causeInput.addEventListener('input', () => { draft.possible_causes = causeInput.value; markDirty('possible_causes'); });

  const uncertainBox = h('input', { type: 'checkbox', checked: uncertain });
  uncertainBox.addEventListener('change', () => {
    causeInput.disabled = uncertainBox.checked;
    if (uncertainBox.checked) causeInput.value = '';
    markDirty('possible_causes');
  });

  const adviceInput = h('textarea', { rows: 4 });
  adviceInput.value = draft.advice_text;
  adviceInput.addEventListener('input', () => { draft.advice_text = adviceInput.value; markDirty('advice_text'); });

  const studentSelect = h('select', {}, data.student_options.map((s) =>
    h('option', { value: s.id, selected: s.id === draft.student_id }, studentLabel(s))));
  studentSelect.addEventListener('change', () => { draft.student_id = Number(studentSelect.value); markDirty('student_id'); });

  const occurredInput = h('input', { type: 'date', value: draft.occurred_on });
  occurredInput.addEventListener('change', () => { draft.occurred_on = occurredInput.value; markDirty('occurred_on'); });

  const sourceTypeInput = h('select', {}, ['作业', '考试', '练习', '其他'].map((t) =>
    h('option', { value: t, selected: t === draft.source_type }, t)));
  sourceTypeInput.addEventListener('change', () => { draft.source_type = sourceTypeInput.value; markDirty('source_type'); });

  const sourceDetailInput = h('input', { type: 'text' });
  sourceDetailInput.value = draft.source_detail;
  sourceDetailInput.addEventListener('input', () => { draft.source_detail = sourceDetailInput.value; markDirty('source_detail'); });

  const teacherNoteInput = h('textarea', { rows: 2, placeholder: '给家长或其他老师看的说明（可选）' });
  teacherNoteInput.value = draft.teacher_note;
  teacherNoteInput.addEventListener('input', () => { draft.teacher_note = teacherNoteInput.value; markDirty('teacher_note'); });

  function collectPayload() {
    const payload = {};
    const listFields = { knowledge_points: ',', observed_errors: '\n', possible_causes: '\n' };
    for (const key of dirty) {
      if (key === 'knowledge_points') {
        payload[key] = draft[key].split(/[，,]/).map((s) => s.trim()).filter(Boolean);
      } else if (key === 'observed_errors' || key === 'possible_causes') {
        payload[key] = uncertainBox.checked && key === 'possible_causes'
          ? ['暂不确定']
          : draft[key].split('\n').map((s) => s.trim()).filter(Boolean);
      } else if (key === 'student_id') {
        payload[key] = Number(draft[key]);
      } else {
        payload[key] = draft[key];
      }
    }
    return payload;
  }

  async function save(quiet) {
    if (!dirty.size) { if (!quiet) toast('没有修改'); return true; }
    try {
      await api('/api/mistakes/' + data.id, { method: 'PATCH', body: collectPayload() });
      dirty.clear();
      if (!quiet) toast('已保存');
      return true;
    } catch (err) {
      toast(err.message, true);
      return false;
    }
  }

  const saveButton = h('button', {
    class: 'btn',
    onclick: async () => { if (await save()) renderRoute(); },
  }, '保存修改');

  const confirmButton = h('button', {
    class: 'btn ok',
    onclick: async () => {
      if (!(await save(true))) return;
      try {
        await api('/api/mistakes/' + data.id + '/confirm', { method: 'POST' });
        toast('已确认，将计入统计');
        renderRoute();
      } catch (err) { toast(err.message, true); }
    },
  }, '确认无误，计入统计');

  const returnButton = h('button', {
    class: 'btn danger',
    onclick: async () => {
      const result = await formDialog({
        title: '退回收到的错题',
        fields: [{ name: 'reason', label: '退回原因（可选）', type: 'textarea', rows: 2 }],
        confirmText: '退回',
        onSubmit: (values) => api('/api/mistakes/' + data.id + '/return', { method: 'POST', body: values }),
      });
      if (result) { toast('已退回，不计入统计'); renderRoute(); }
    },
  }, '退回');

  const reopenButton = h('button', {
    class: 'btn',
    onclick: async () => {
      const result = await formDialog({
        title: '重新编辑',
        fields: [{ name: 'reason', label: '说明（可选）', type: 'textarea', rows: 2 }],
        confirmText: '重新打开',
        onSubmit: (values) => api('/api/mistakes/' + data.id + '/reopen', { method: 'POST', body: values }),
      });
      if (result) { toast('已回到「待确认错题」，暂不计入统计'); renderRoute(); }
    },
  }, '重新编辑');

  const reanalyze = async () => {
    const real = aiMode() === 'real';
    const ok = await confirmDialog({
      title: '重新分析',
      message: real
        ? '将再次把当前照片与题目文字发送给阿里云百炼（可能产生费用），并用新结果覆盖当前 AI 内容（老师已修改的字段也会被替换）。继续？'
        : '将用当前的题目与解题过程文字重新生成模拟分析，会覆盖未保存的 AI 内容（老师已修改的字段也会被替换）。继续？',
      confirmText: '重新分析',
    });
    if (!ok) return;
    try {
      const result = await api('/api/mistakes/' + data.id + '/analyze', {
        method: 'POST',
        body: { consent: real },
      });
      if (result.ai_error) {
        toast('分析失败：' + result.ai_error, true);
      } else {
        toast('已重新生成分析，请核对');
      }
      renderRoute();
    } catch (err) { toast(err.message, true); }
  };

  const statusButtons = data.status === 'pending'
    ? [confirmButton, returnButton]
    : [reopenButton];

  $app.replaceChildren(h('div', {},
    h('div', { class: 'small' }, h('a', { href: '#/class/' + data.class_id + '/mistakes' }, '← 返回错题列表')),
    h('div', { class: 'row between', style: 'margin:6px 0 12px' },
      h('div', {},
        h('h2', {}, studentText(data.student_name, data.student_note), ' ', statusChip(data.status)),
        h('div', { class: 'small muted' }, data.class_name + ' · ' + data.source_type +
          (data.source_detail ? ' · ' + data.source_detail : '') + ' · ' + fmtDate(data.occurred_on)),
      ),
      h('div', { class: 'row' }, saveButton, ...statusButtons),
    ),
    data.ai_error
      ? h('div', { class: 'banner warn' },
        h('b', {}, '这次没有生成分析内容：'), '下面的字段是空的。照片、题目和解题过程都已保存，可以重试，也可以手动填写；',
        '只有你确认后才计入统计。')
      : data.ai_mode === 'local'
        ? h('div', { class: 'banner mock' },
          h('b', {}, '本地录入：'), '这条错题由桌面版手动录入，没有调用 AI 分析，字段内容都来自老师填写；',
          '只有你点「确认无误」后，这条记录才会计入统计。')
        : data.ai_mode === 'mock'
        ? h('div', { class: 'banner mock' },
          h('b', {}, '模拟分析：'), '本页题目与解题过程未被任何图像识别读取，内容来自你填写的文字或内置虚构示例，',
          '请对照左侧原图逐项核对；只有你点「确认无误」后，这条记录才会计入统计。')
        : h('div', { class: 'banner real' },
          h('b', {}, '真实 AI 分析：'), '以下内容由阿里云百炼读取照片生成，只是建议。',
          '「观察到的错误」是模型声称从照片看到的内容，「可能原因」是推测，两者请分别核对；只有你确认后才计入统计。'),
    data.ai_error ? h('div', { class: 'banner danger' },
      h('b', {}, '分析失败：'), data.ai_error, h('br'),
      h('span', { class: 'small' }, '照片、题目和解题过程都已保存，可以重试或手动填写。真实 AI 模式下重试会再次调用服务，可能产生费用。'),
      h('div', { style: 'margin-top:8px' },
        h('button', { class: 'btn sm', onclick: () => reanalyze() }, '重试分析'))) : null,
    data.likely_correct ? h('div', { class: 'banner warn' },
      h('b', {}, '可能没有错误：'), 'AI 认为这道题的解答看起来是正确的，请对照原图核对，不要直接当作错误结论。') : null,
    data.insufficient ? h('div', { class: 'banner danger' },
      h('b', {}, '信息不足：'), data.insufficient_reason || '系统没有足够信息给出确定结论。') : null,
    h('div', { class: 'review' },
      h('div', { class: 'image-pane' },
        h('div', { class: 'card' },
          h('div', { class: 'row between', style: 'margin-bottom:8px' },
            h('h3', {}, '原图'),
            h('span', { class: 'tiny muted' }, data.image_width + '×' + data.image_height)),
          data.has_image
            ? h('img', { src: '/api/mistakes/' + data.id + '/image', alt: '学生错题原图' })
            : h('div', { class: 'muted' }, '这条记录没有图片'),
          h('p', { class: 'tiny muted', style: 'margin-top:8px' },
            '图片只保存在本机 data/uploads 目录，只有登录后的老师能访问。'),
        ),
        h('div', { class: 'card' },
          h('h3', {}, '录入信息'),
          h('label', { class: 'field', style: 'margin-bottom:8px' }, h('span', { class: 'lab' }, '所属学生'), studentSelect),
          h('div', { class: 'grid-2' },
            h('label', { class: 'field' }, h('span', { class: 'lab' }, '来源'), sourceTypeInput),
            h('label', { class: 'field' }, h('span', { class: 'lab' }, '错题日期'), occurredInput),
          ),
          h('label', { class: 'field', style: 'margin-top:8px' }, h('span', { class: 'lab' }, '来源说明'), sourceDetailInput),
          h('label', { class: 'field', style: 'margin-top:8px' }, h('span', { class: 'lab' }, '老师备注'), teacherNoteInput),
        ),
      ),
      h('div', {},
        h('div', { class: 'card' },
          h('div', { class: 'field-block' },
            h('div', { class: 'field-label' }, h('span', { class: 'lab' }, '① 识别出的题目'), fieldBadge('question_text')),
            questionInput,
            aiHint('question_text'),
          ),
          h('div', { class: 'field-block' },
            h('div', { class: 'field-label' }, h('span', { class: 'lab' }, '② 学生的解题过程'), fieldBadge('solution_text')),
            solutionInput,
            aiHint('solution_text'),
          ),
          h('div', { class: 'field-block' },
            h('div', { class: 'field-label' }, h('span', { class: 'lab' }, '③ 涉及的数学知识点'), fieldBadge('knowledge_points')),
            kpInput,
            h('div', { class: 'row', style: 'margin-top:6px;gap:6px' },
              (state.meta ? state.meta.knowledge_points : []).map((kp) => h('button', {
                class: 'chip', type: 'button',
                onclick: () => {
                  const parts = kpInput.value.split(/[，,]/).map((s) => s.trim()).filter(Boolean);
                  if (!parts.includes(kp)) parts.push(kp);
                  kpInput.value = parts.join('，');
                  draft.knowledge_points = kpInput.value;
                  markDirty('knowledge_points');
                },
              }, '＋' + kp))),
            aiHint('knowledge_points', (value) => (Array.isArray(value) ? value.join('、') : value)),
          ),
          h('div', { class: 'field-block' },
            h('div', { class: 'field-label' }, h('span', { class: 'lab' }, '④ 错误类型'), fieldBadge('error_type')),
            errorSelect,
            aiHint('error_type'),
          ),
        ),
        h('div', { class: 'card' },
          h('div', { class: 'banner info', style: 'margin-bottom:12px' },
            h('b', {}, '区分「观察到的错误」和「推测的原因」：'),
            '上半部分只写照片或解题过程里能直接看到的现象；下半部分是推测，可能不准，也可以直接选「暂不确定」。'),
          h('div', { class: 'field-block' },
            h('div', { class: 'field-label' }, h('span', { class: 'lab' }, '⑤ 观察到的错误（客观现象）'), fieldBadge('observed_errors')),
            observedInput,
            aiHint('observed_errors', (value) => (Array.isArray(value) ? value.join('；') : value)),
          ),
          h('div', { class: 'field-block' },
            h('div', { class: 'field-label' }, h('span', { class: 'lab' }, '⑥ 可能原因（推测）'), fieldBadge('possible_causes')),
            causeInput,
            h('label', { class: 'row small', style: 'margin-top:6px' }, uncertainBox, '暂不确定（先不下结论）'),
            aiHint('possible_causes', (value) => (Array.isArray(value) ? value.join('；') : value)),
          ),
          h('div', { class: 'field-block', style: 'margin-bottom:0' },
            h('div', { class: 'field-label' }, h('span', { class: 'lab' }, '⑦ 针对这道题的教学 / 学习建议'), fieldBadge('advice_text')),
            adviceInput,
            aiHint('advice_text'),
          ),
        ),
        h('div', { class: 'card' },
          h('div', { class: 'row between' },
            h('span', { class: 'small muted' },
              data.confirmed_at ? '确认时间：' + data.confirmed_at.slice(0, 16).replace('T', ' ') : '尚未确认'),
            h('button', { class: 'btn sm ghost', onclick: reanalyze }, '重新分析'),
          ),
          data.returned_reason ? h('div', { class: 'ai-note' }, '退回原因：' + data.returned_reason) : null,
        ),
      ),
    ),
  ));
}

/* ------------------------------------------------------------------ */
/* 设置                                                                */
/* ------------------------------------------------------------------ */
async function renderSettings() {
  // R1-14/T17：个体 AI 开关的初始状态**必须来自后端**（GET /api/account/ai-individual），
  // 不在前端假设默认值；读不到就把错误原样显示，并且不让人在「不知道真实状态」时乱点保存。
  let individualAi = null;
  let individualAiError = '';
  try {
    individualAi = await api('/api/account/ai-individual');
  } catch (err) {
    individualAiError = (err && err.message) || '读取开关状态失败';
  }

  const oldPwd = h('input', { type: 'password', autocomplete: 'current-password' });
  const newPwd = h('input', { type: 'password', autocomplete: 'new-password' });
  const errorLine = h('div', { class: 'small', style: 'color:var(--danger);min-height:20px' }, '');

  $app.replaceChildren(h('div', {},
    h('div', { class: 'small' }, h('a', { href: '#/classes' }, '← 我的班级')),
    h('h2', { style: 'margin:6px 0 14px' }, '设置'),
    h('div', { class: 'card stack' },
      h('h3', {}, '修改密码'),
      h('p', { class: 'small muted' }, '演示账号的初始密码是公开的，正式使用前请改掉。'),
      h('label', { class: 'field' }, h('span', { class: 'lab' }, '当前密码'), oldPwd),
      h('label', { class: 'field' }, h('span', { class: 'lab' }, '新密码（至少 8 位）'), newPwd),
      errorLine,
      h('button', {
        class: 'btn primary',
        onclick: async () => {
          errorLine.textContent = '';
          try {
            await api('/api/auth/password', { method: 'POST', body: { old_password: oldPwd.value, new_password: newPwd.value } });
            toast('密码已修改');
            oldPwd.value = ''; newPwd.value = '';
          } catch (err) { errorLine.textContent = err.message; }
        },
      }, '保存新密码'),
    ),
    h('div', { class: 'card stack' },
      h('h3', {}, 'AI 分析'),
      h('p', { class: 'small' },
        aiMode() === 'real'
          ? ['当前：', h('b', {}, '真实 AI 分析'), '（阿里云百炼 / ' + aiModelName() + '）。每张照片发送前都要勾选同意。']
          : ['当前：', h('b', {}, '模拟分析'), '。' + (state.meta && state.meta.ai_reason ? state.meta.ai_reason : '')]),
      h('p', { class: 'small muted' },
        '密钥只从环境变量读取，不写入源码、数据库或页面。设置 DASHSCOPE_API_KEY 后重启服务即可启用；'
        + '也可设置 MISTAKE_AI_MODE=mock 在有密钥时仍只做模拟分析。'),
      h('p', { class: 'small muted' }, state.meta ? state.meta.ai_price_note : ''),
      h('p', { class: 'small' },
        '发送内容：' + (state.meta ? state.meta.ai_send_fields.join('、') : '错题照片、题目文字、解题过程')
        + '；不发送学生姓名与班级名。'),
      state.meta && state.meta.ai_policy_summary
        ? h('p', { class: 'small muted' }, state.meta.ai_policy_summary, ' ',
          h('a', { href: state.meta.ai_policy_url, target: '_blank', rel: 'noopener' }, '官方说明'))
        : null,
    ),
    individualAiCard(individualAi, individualAiError),
    h('div', { class: 'card stack' },
      h('h3', {}, '数据与隐私'),
      h('ul', { class: 'small', style: 'padding-left:18px;line-height:1.9' },
        h('li', {}, '班级、学生、错题、照片保存在本机 data/ 目录；真实 AI 模式下，勾选同意的照片与文字会发送给阿里云百炼。'),
        h('li', {}, '模拟分析不读取图片像素，也不会把任何数据发送出去。'),
        h('li', {}, '登录后才能访问数据；照片通过需要登录的接口返回，不在静态目录里直接暴露。'),
        h('li', {}, '清空班级会删除该班学生、错题、照片和统计，需要输入班级名称二次确认；它无法删除对方服务端已接收的数据。'),
      ),
    ),
    dataCard(),
  ));
}

// R1-14/T17：账号级「个体 AI 外发」开关。网页端只能改偏好（服务端 ai_egress_available=false），
// 真正外发只在注入了 provider 的桌面端生效——所以这里既要说清「能改」，也要说清「不一定能发」。
function individualAiCard(state, loadError) {
  const info = state || {};
  const allowed = info.ai_individual_allowed === true;
  const egressAvailable = info.ai_egress_available === true;
  const statusLine = h('p', { class: 'small ai-status' }, '当前状态：',
    h('b', {}, allowed ? '已开启' : '已关闭'));
  const toggle = h('input', { type: 'checkbox' });
  toggle.checked = allowed;
  const receipt = h('p', { class: 'small ai-receipt', style: 'margin:6px 0 0' }, '');
  const error = h('p', { class: 'small ai-error', style: 'color:var(--danger);margin:6px 0 0' }, '');
  if (loadError) {
    error.textContent = '读取开关状态失败：' + loadError + '。刷新页面后可重试；在读到真实状态前请不要保存开关。';
  }
  const saveBtn = h('button', {
    class: 'btn ai-save',
    disabled: Boolean(loadError),
    onclick: async () => {
      error.textContent = '';
      receipt.textContent = '';
      saveBtn.disabled = true;
      try {
        // 保存走 POST（服务端没有 do_PUT，PUT 会 405）。
        const result = await api('/api/account/ai-individual',
          { method: 'POST', body: { allowed: !!toggle.checked } }) || {};
        const now = result.ai_individual_allowed === true;
        // 回执只读服务端返回：不拿请求里的值冒充结果。
        toggle.checked = now;
        statusLine.replaceChildren('当前状态：', h('b', {}, now ? '已开启' : '已关闭'));
        receipt.textContent = '已保存：' + (now ? '已开启' : '已关闭') + '（服务端回执）'
          + (result.note ? ' ' + result.note : '');
        toast(now ? '已开启个体 AI 外发' : '已关闭个体 AI 外发');
      } catch (err) {
        error.textContent = '保存失败：' + ((err && err.message) || '请稍后重试')
          + (err && err.code ? '（' + err.code + '）' : '') + '。本次没有改动开关。';
      } finally {
        saveBtn.disabled = false;
      }
    },
  }, '保存开关');

  return h('div', { class: 'card stack ai-individual' },
    h('h3', {}, '个体 AI 外发（账号级开关）'),
    statusLine,
    h('label', { class: 'row', style: 'gap:8px;align-items:center;margin:6px 0' },
      toggle, h('span', {}, '允许个体诊断使用我配置的 AI 通道')),
    h('p', { class: 'tiny muted' }, egressAvailable
      ? '这台机器（桌面端）可以真的外发；开关是账号级的，网页与桌面端读同一个值。'
      : '网页端只记录你的选择、不能外发：真正的外发只在桌面端生效（同一账号在桌面端会读到这个开关）。'),
    h('ul', { class: 'small', style: 'padding-left:18px;line-height:1.9;margin:6px 0' },
      h('li', {}, '打开后会把：题目原文、学生步骤、题图发给你配置的 AI 通道'
        + '（错题重新分析、错题照片分析建议、个人辅导建议、追问候选、追问后的错因候选、作答分析）。'),
      h('li', {}, '不会发：姓名、学号、班级名、作答编号（answer_id）；外发字段只允许 '
        + 'messages / model / temperature / max_tokens。'),
      h('li', {}, '每次真实外发都会写一条审计（谁、何时、哪个功能、发了哪些字段）；'
        + '审计只记字段名、不记取值，写不进审计就不发送。'),
      h('li', {}, '关闭后六个入口立即恢复拒绝（错误码 ai_isolation），不需要重启。'),
    ),
    h('p', { class: 'tiny muted' }, '这是账号级开关：在这里打开后，同一账号在桌面端也生效；'
      + '桌面端不显示这个开关。'),
    h('div', { class: 'check-actions', style: 'margin-top:6px' }, saveBtn),
    receipt,
    error,
  );
}

function dataCard() {
  const meta = state.meta || {};
  const portable = Boolean(meta.portable);
  const status = h('div', { class: 'small muted' }, '');
  const dataLine = h('p', { class: 'small' }, '数据目录：', h('code', {}, meta.data_dir || 'data/'));

  const backupButton = h('button', {
    class: 'btn',
    onclick: async () => {
      status.textContent = '正在打包备份…';
      try {
        await downloadBackup();
        status.textContent = '备份已下载；里面有数据库和全部错题照片。';
      } catch (err) {
        status.textContent = err.message;
      }
    },
  }, '下载备份（含照片）');

  const restoreInput = h('input', { type: 'file', accept: '.zip,application/zip', style: 'display:none' });
  restoreInput.addEventListener('change', async () => {
    const file = restoreInput.files && restoreInput.files[0];
    restoreInput.value = '';
    if (!file) return;
    const ok = await confirmDialog({
      title: '从备份恢复',
      message: [
        h('p', {}, '恢复会用备份里的班级、学生、错题和照片，覆盖当前账号在本机保存的全部数据。'),
        h('p', {}, '恢复前会自动把当前数据备份到 data/backups/，但仍建议你先自己下载一份。'),
        h('p', {}, '备份会先做完整性校验；失败时不会改动现有数据。恢复完成后软件会自动重启，需要重新登录。'),
      ],
      confirmText: '校验并恢复',
      danger: true,
    });
    if (!ok) return;
    status.textContent = '正在上传并校验备份…';
    try {
      const result = await uploadZip('/api/restore', file, async (error, data) => {
        // 1.2.1 及更早的备份没有唯一身份标识，只能凭账号名+编号匹配，必须再次确认
        if (error.status === 409 && data.code === 'legacy_confirm_required') {
          const sure = await confirmDialog({
            title: '旧版备份，请确认归属',
            message: [
              h('p', {}, '这份备份来自 ' + (data.owner_username || '未知账号') + '，是旧版软件导出的，无法核对唯一身份，只能凭账号名和编号判断。'),
              h('p', {}, '请确认它确实是你本人在当前账号下的备份。确认后会用其中的数据覆盖当前账号的全部数据。'),
            ],
            confirmText: '确认是我的备份',
            danger: true,
          });
          if (!sure) return null;
          return await api('/api/restore/confirm', { method: 'POST', body: { task: data.task, confirm_legacy: true } });
        }
        throw error;
      });
      if (result === null) {
        status.textContent = '已取消恢复，数据没有改动。';
        return;
      }
      status.textContent = '备份已通过校验，正在恢复并重启，请稍候…';
      await waitForRestart();
    } catch (err) {
      status.textContent = err.message;
    }
  });

  const importInput = h('input', { type: 'file', accept: '.zip,application/zip', style: 'display:none' });
  importInput.addEventListener('change', async () => {
    const file = importInput.files && importInput.files[0];
    importInput.value = '';
    if (!file) return;
    status.textContent = '正在上传并校验备份…';
    let preview;
    try {
      preview = await uploadZip('/api/import', file);
    } catch (err) {
      status.textContent = err.message;
      return;
    }
    const counts = preview.counts || {};
    const message = [
      h('p', {}, '备份来源账号：' + (preview.source_username || '未知账号')
        + '（备份创建于 ' + (preview.created_at || '未知时间') + '）。'),
      h('p', {}, '包含 ' + (counts.classes || 0) + ' 个班级、' + (counts.students || 0) + ' 名学生、'
        + (counts.mistakes || 0) + ' 条错题、' + (counts.photos || 0) + ' 张照片。'),
      h('p', {}, '导入后，这些数据会归属到当前登录账号；不会导入备份里的密码或登录状态，也不影响其他账号的数据。'),
      preview.legacy
        ? h('p', {}, '这是旧版软件导出的备份，无法核对唯一身份，请确认它确实来自你本人的旧电脑。')
        : null,
      h('p', {}, '导入前会自动备份当前数据；当前账号必须是还没有班级的新账号。'),
    ];
    const ok = await confirmDialog({ title: '从其他电脑导入备份', message, confirmText: '确认导入', danger: true });
    if (!ok) {
      status.textContent = '已取消导入，数据没有改动。';
      return;
    }
    status.textContent = '正在导入并重启，请稍候…';
    try {
      await api('/api/import/confirm', { method: 'POST', body: { task: preview.task, confirm: true } });
      await waitForRestart();
    } catch (err) {
      status.textContent = err.message;
    }
  });

  const restoreButton = h('button', {
    class: 'btn',
    disabled: !portable,
    title: portable ? '' : '一键恢复只在便携版启动器里可用',
    onclick: () => restoreInput.click(),
  }, '从备份恢复');

  const importButton = h('button', {
    class: 'btn',
    disabled: !portable,
    title: portable ? '' : '一键导入只在便携版启动器里可用',
    onclick: () => importInput.click(),
  }, '从其他电脑导入备份');

  const shutdownButton = portable
    ? h('button', {
      class: 'btn danger',
      onclick: async () => {
        const ok = await confirmDialog({ title: '关闭软件', message: [h('p', {}, '关闭后后台服务会停止，正在拍摄或上传的内容会中断。')], confirmText: '关闭', danger: true });
        if (!ok) return;
        try {
          await api('/api/shutdown', { method: 'POST' });
          status.textContent = '软件已关闭，可以关掉这个页面。';
        } catch (err) {
          status.textContent = err.message;
        }
      },
    }, '关闭软件')
    : null;

  const blockedBanner = meta.restore_blocked
    ? h('div', { class: 'banner real' },
      h('b', {}, '数据保护：'), ' 上次恢复中断后数据状态不确定，软件已停止保存修改。',
      '请保留整个软件文件夹，恢复前的数据在 data/backups/ 目录，联系技术人员处理。')
    : null;

  const last = meta.last_restore;
  let lastLine = null;
  if (last && last.ok === false) {
    let text = '上次恢复：未完成，原数据未改动。';
    if (last.phase === 'unknown') text = '上次恢复：中断后数据状态不确定，已停止保存修改。';
    else if (last.phase === 'finalize_failed') text = '上次恢复：数据已恢复，但收尾没有全部完成。';
    else if (last.phase === 'rolled_back') text = '上次恢复：修改已回滚，原数据未改动。';
    lastLine = h('p', { class: 'small muted' }, text + (last.error ? '（' + last.error + '）' : ''));
  } else if (last && last.ok === true) {
    lastLine = h('p', { class: 'small muted' }, '上次恢复：成功' + (last.at ? '（' + last.at + '）' : '') + '。');
  }

  return h('div', { class: 'card stack' },
    h('h3', {}, '本机数据与备份'),
    h('p', { class: 'small' },
      '账号、班级、学生、错题和照片都保存在这台电脑的软件文件夹里。换一台电脑不会自动同步；',
      '需要迁移时，请在新电脑注册账号后，用「从其他电脑导入备份」。'),
    dataLine,
    h('p', { class: 'small muted' }, 'AI 分析不是在本机运行的：真实模式会把勾选同意的照片与文字发到阿里云百炼，模拟模式不发任何数据。'),
    h('div', { style: 'display:flex;gap:10px;flex-wrap:wrap;align-items:center' },
      backupButton,
      restoreButton,
      importButton,
      shutdownButton,
    ),
    restoreInput,
    importInput,
    blockedBanner,
    lastLine,
    status,
  );
}

async function downloadBackup() {
  const resp = await fetch('/api/backup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
    credentials: 'same-origin',
  });
  if (!resp.ok) {
    let message = '备份失败，请稍后重试。';
    try { message = (await resp.json()).error || message; } catch (err) { /* 非 JSON 错误 */ }
    throw new Error(message);
  }
  const blob = await resp.blob();
  const disposition = resp.headers.get('Content-Disposition') || '';
  const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const name = match ? decodeURIComponent(match[1]) : '错题数据备份.zip';
  const url = URL.createObjectURL(blob);
  const link = h('a', { href: url, download: name });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

async function uploadZip(path, file, onReject) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/zip' },
    body: file,
    credentials: 'same-origin',
  });
  let data = {};
  try { data = await resp.json(); } catch (err) { /* 服务可能正在重启 */ }
  if (!resp.ok) {
    const error = new Error(data.error || '备份上传失败');
    error.status = resp.status;
    error.code = data.code || '';
    if (onReject) return onReject(error, data);
    throw error;
  }
  return data;
}

async function waitForRestart() {
  for (let i = 0; i < 80; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 800));
    try {
      const resp = await fetch('/api/info', { cache: 'no-store' });
      if (resp.ok) {
        location.hash = '#/login';
        location.reload();
        return;
      }
    } catch (err) { /* 服务尚未恢复 */ }
  }
  toast('恢复后服务没有重新出现，请关闭软件再打开。', true);
}

/* ------------------------------------------------------------------ */
/* 作答核对与追问（A1–A6）                                              */
/* 键名只按 docs/作答核对_接口契约.md §3 读；缺字段一律用契约默认值兜底，  */
/* 并在 docs/web界面自测.md 记一条，不在前端自己发明后端字段。            */
/* ------------------------------------------------------------------ */

// R1-06：不再有「写死的去括号问句」兜底。问句一律取服务端 followup.suggestion（按本条错步生成），
// 没有依据时服务端给 available=false + 「暂无适用追问」，这里只照着显示，不自己编一句。
const CHECK_ASK_UNAVAILABLE = '暂无适用追问';
// R1-T12：批次选择里的「全部批次」。它只是**本地视图标记**，不会当成批次名发给后端
// （后端没有叫「全部批次」的批次，`?batch=*all*` 只会查到 0 份）；选它时页面照常要
// 「当前作业 + 各批次清单」，再把各批次并列相加，并写明它和「当前作业」的区别。
const CHECK_ALL_BATCHES = '*all*';
const CHECK_PRESET_NOTE = '预置演示 · 不代表已实现通用定位';
const CHECK_QUEUE_NOTE = '保存后这份作答退出待核对队列，但仍可在「查看核对记录」里按「原因待判断」找回。';
const CHECK_LONG_LINES = 10;

function checkNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

// 老师正在填的内容留一份在内存里：保存失败时不重渲染，重渲染也不丢输入。
function checkDraft(answerId) {
  const key = String(answerId);
  if (!state.ui.checkDrafts[key]) state.ui.checkDrafts[key] = {};
  return state.ui.checkDrafts[key];
}

// A3：处理进度与原因判断是两条轴，各占一个标签；中间那句分隔串只是给人看的。
// 原因轴三态（R1-02）：待判断 / 教师已判断 / 无需判断。文案一律以服务端 label 为准，
// 本地兜底只为「字段缺失」时不至于显示空白。
function checkAxisChips(judgement) {
  const j = judgement || {};
  const progressLabel = j.progress_label || (j.progress === 'checked' ? '已核对' : '待核对');
  const fallbackCause = j.cause_state === 'decided'
    ? '教师已判断'
    : (j.cause_state === 'not_applicable' ? '无需判断' : '原因待判断');
  const causeLabel = j.cause_label || fallbackCause;
  return h('div', { class: 'axis-row' },
    h('span', { class: 'axis-chip progress' + (j.progress === 'checked' ? ' on' : '') }, progressLabel),
    h('span', { class: 'axis-sep' }, ' · '),
    h('span', {
      class: 'axis-chip cause'
        + (j.cause_state === 'decided' ? ' on' : '')
        + (j.cause_state === 'not_applicable' ? ' na' : ''),
    }, causeLabel),
  );
}

// 错因来源说人话（R1-02「区分人工填写与候选采纳」）：none / preset / teacher / legacy。
// 人工判断允许只填说明不填编码，所以判据是「有没有结论」而不是「有没有编码」。
function checkCauseSourceText(judgement) {
  const j = judgement || {};
  const label = j.cause_source_label || '';
  if ((j.cause_code || j.cause_label_text) && label) return '错因来源：' + label;
  return '';
}

function checkEvidenceList(title, items) {
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!list.length) return null;
  return h('div', { class: 'evi' },
    h('div', { class: 'evi-title' }, title),
    h('ul', { class: 'evi-list' }, list.map((item) => h('li', {}, String(item)))),
  );
}

// 逐行学生原文：契约给 lines；缺 lines 时按 student_steps 换行兜底（记进自测文档）。
function checkLines(loc, answer) {
  if (Array.isArray(loc.lines) && loc.lines.length) return loc.lines;
  const raw = String(answer.student_steps || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return raw.map((text, index) => ({ text, role: index === 0 ? 'given' : 'student', bad: false }));
}

// 错步行：下划线 + 文字标记同时出现（A2/A6：不能只靠颜色）。
function checkStepText(text, isBad, loc) {
  const raw = String(text === undefined || text === null ? '' : text);
  if (!isBad) return h('span', { class: 'step-math' }, raw);
  const hint = loc.hint_text ? String(loc.hint_text) : '';
  const underline = loc.underline || {};
  const start = Number(underline.start);
  const end = Number(underline.end);
  if (hint && Number.isInteger(start) && Number.isInteger(end) && raw.slice(start, end) === hint) {
    return h('span', { class: 'step-math bad-math' },
      raw.slice(0, start),
      h('u', { class: 'bad-underline' }, hint),
      raw.slice(end),
    );
  }
  if (hint && raw.indexOf(hint) >= 0) {
    const at = raw.indexOf(hint);
    return h('span', { class: 'step-math bad-math' },
      raw.slice(0, at),
      h('u', { class: 'bad-underline' }, hint),
      raw.slice(at + hint.length),
    );
  }
  return h('span', { class: 'step-math bad-math' }, h('u', { class: 'bad-underline' }, raw));
}

function checkLineRow(line, index, loc, isBad) {
  const role = line.role === 'given' ? '原题' : '第 ' + index + ' 行';
  return h('div', { class: 'step-line' + (isBad ? ' bad' : '') },
    h('span', { class: 'rec-no' }, role),
    checkStepText(line.text, isBad, loc),
    isBad ? h('span', { class: 'step-mark' }, '首处不等价') : null,
  );
}

// 定位状态说人话：不判错的时候必须讲清「为什么不标错」。
// R1-04 移交的文案分句：missing_steps 现在至少分三句——没有过程 / 只写了结论行 /
// 有过程但没解完（steps.kind + completion.state）。服务端已经给出 `notice` 时以它为准
// （单一事实来源），这里的本地分支只作兜底，字段缺失一律 .get 语义（undefined 安全）。
function checkLocateNotice(loc, notice) {
  const status = loc.status || '';
  if (status === 'first_bad') return null;
  const serverNotice = String(notice || '').trim();
  if (serverNotice) {
    const kindClass = status === 'correct' ? 'info' : 'warn';
    return h('div', { class: 'banner ' + kindClass }, serverNotice);
  }
  if (status === 'correct') {
    return h('div', { class: 'banner info' }, '全程等价：从题干到学生最后一行都没有发现不等价，不标错步。');
  }
  if (status === 'missing_steps') {
    const steps = loc.steps || {};
    const completion = loc.completion || {};
    const kind = steps.kind || 'empty';
    let sentence = '缺少步骤：这份没有可核对的过程，系统不猜错在哪一行。';
    if (kind === 'answer_only') {
      sentence = '缺少步骤：学生只写了结论行，无法定位具体错在哪一行，系统不猜。';
    } else if (kind === 'unparsable' || kind === 'unchecked') {
      sentence = '缺少步骤：写出的过程没能解析成可核对的式子，系统不猜错在哪一行。';
    } else if (completion.state === 'unfinished') {
      sentence = '解题未完成：有过程但最后一行还不是结果，系统不猜错在哪一行。';
    } else if (kind === 'empty') {
      sentence = '缺少步骤：这份的步骤栏里没有可核对的过程，系统不猜错在哪一行。';
    }
    return h('div', { class: 'banner warn' }, sentence, loc.manual_note ? ' ' + loc.manual_note : '');
  }
  if (status === 'missing') {
    return h('div', { class: 'banner warn' },
      '缺测：这份作答没有提交可核验的步骤，系统不判对错、也不猜错因。',
      loc.reason_note ? ' ' + loc.reason_note : '');
  }
  if (status === 'out_of_scope') {
    return h('div', { class: 'banner warn' },
      '超出本轮核验范围：已转人工，原步骤照常完整显示，不标错步。',
      loc.manual_note ? ' ' + loc.manual_note : '');
  }
  return h('div', { class: 'banner warn' }, '定位结果没有返回，按人工核对处理：原步骤照常显示，不标错步。');
}

function checkContrast(loc) {
  const contrast = loc.contrast || {};
  return h('div', {},
    h('div', { class: 'contrast' },
      h('div', { class: 'kv' },
        h('div', { class: 'k' }, '按规则应为'),
        h('div', { class: 'v' }, contrast.expected || '—')),
      h('div', { class: 'kv' },
        h('div', { class: 'k' }, '学生这一行'),
        h('div', { class: 'v' }, contrast.actual || '—')),
    ),
    contrast.note ? h('p', { class: 'small', style: 'margin-top:8px' }, '对照：' + contrast.note) : null,
  );
}

function checkStdSolution(answer) {
  const raw = String(answer.standard_solution || '').trim();
  const steps = raw.split(/[；;\n]+/).map((item) => item.trim()).filter(Boolean);
  return h('details', { class: 'std-sol' },
    h('summary', {}, '标准解（默认折叠，点击展开）'),
    steps.length
      ? h('ol', { class: 'std-list' }, steps.map((step) => h('li', { class: 'step-math sm' }, step)))
      : h('p', { class: 'small muted' }, '这份作答没有附带标准解。'),
  );
}

// A1 次级入口要真的能用：新建班级走同一个接口，建完直接进新班级的作答核对。
async function promptCreateClass() {
  const result = await formDialog({
    title: '新建班级',
    fields: [
      { name: 'name', label: '班级名称', required: true, placeholder: '例如：七年级 1 班' },
      { name: 'grade', label: '年级（可选）', placeholder: '例如：七年级' },
      { name: 'note', label: '备注（可选）' },
    ],
    confirmText: '创建',
    onSubmit: (values) => api('/api/classes', { method: 'POST', body: values }),
  });
  if (!result) return null;
  toast('班级已创建');
  if (result.id) location.hash = '#/class/' + result.id + '/check';
  return result;
}

function renderCheckTab(classData, sub, query) {
  if (sub === 'records') return renderCheckRecords(classData, query || {});
  const answerId = Number(sub);
  if (sub && Number.isFinite(answerId) && answerId > 0) return renderCheckAnswer(classData, answerId);
  return renderCheckOverview(classData, query || {});
}

/* ---------------- A1：课前总览 ---------------- */
async function renderCheckOverview(classData, query) {
  const batch = query.batch || '';
  const viewAll = batch === CHECK_ALL_BATCHES;
  // 「全部批次」不向后端要一个不存在的批次：照常取「当前作业 + 各批次清单」，
  // 由页面把各批次并列相加（每份作答仍然只属于一个批次）。
  const apiBatch = viewAll ? '' : batch;
  const overview = (await api('/api/classes/' + classData.id + '/check/overview'
    + (apiBatch ? '?batch=' + encodeURIComponent(apiBatch) : ''))) || {};
  const assignments = Array.isArray(overview.assignments) ? overview.assignments : [];
  // 「全部批次」的合计：必须定义在 headline / detail / primary 之前，否则 viewAll 分支会撞
  // const 的 TDZ（ReferenceError），整页渲染不出来（实测「全部批次」页空白）。
  const allTotal = assignments.reduce((sum, item) => sum + checkNum(item.total), 0);
  const allChecked = assignments.reduce((sum, item) => sum + checkNum(item.checked), 0);
  const allPending = assignments.reduce((sum, item) => sum + checkNum(item.pending), 0);

  const overviewState = overview.state || 'no_answers';
  const assignment = overview.assignment || {};
  const next = overview.next || null;
  const assignmentName = assignment.name || assignment.batch || batch || '';
  // 没有任何作答时，「当前作业：未命名批次」会让人误以为要先建作业，干脆不显示。
  const showAssignment = overviewState !== 'no_answers' && assignmentName && assignmentName !== '未命名批次';
  const total = checkNum(assignment.total);
  const pending = checkNum(assignment.pending);
  const checked = checkNum(assignment.checked);
  const missing = checkNum(assignment.missing);
  const nextId = next && next.answer_id !== undefined && next.answer_id !== null ? Number(next.answer_id) : null;

  // A1：两种「没有待核对」的状态必须分开说，谁都不能渲染成空表。
  // 空状态只留一句主文案 + 一句下一步，不再叠加「还没有作答／当前作业／尚未导入」三句同义话。
  const flag = overviewState === 'has_pending'
    ? h('span', { class: 'state-flag pending' }, '待核对作答 ' + pending + ' 份')
    : overviewState === 'all_checked'
      ? h('span', { class: 'state-flag done' }, '全部已核对')
      : null;

  const headline = viewAll
    ? '全部批次：' + assignments.length + ' 个作业、应交 ' + allTotal + ' 份'
    : overviewState === 'no_answers'
    ? '尚未导入作答'
    : overviewState === 'all_checked'
      ? '本次作业已核对完'
      : ((next && next.headline) || '这批作答里，哪些可以判、哪些还需要问一句？');

  const detail = viewAll
    ? '这是把各批次并列相加的结果：已核对 ' + allChecked + ' 份、待核对 ' + allPending
      + ' 份。要看某一批的点下面作业表那一行；「当前作业」范围请在下拉里切回去。'
    : overviewState === 'no_answers'
    ? '可以导入本班作答，或用下面的合成演示数据先走一遍。'
    : overviewState === 'all_checked'
      ? '本次作业共 ' + total + ' 份，已核对 ' + checked + ' 份；原因待判断的记录可以在「查看核对记录」里找回。'
      : '下一份待核对作答：' + ((next && next.student_label) || '—')
        + (next && next.question_no ? ' · 第 ' + next.question_no + ' 题' : '')
        + '。待核对数与「查看待核对作答」用同一个队列。';

  // 唯一主按钮：按当前状态给出唯一一个可点的下一步（A1/A7）。
  const primary = viewAll
    ? h('a', { class: 'btn primary', href: '#/class/' + classData.id + '/check/records' }, '查看核对记录')
    : overviewState === 'no_answers'
    ? h('a', { class: 'btn primary', href: '#/upload?class=' + classData.id }, '导入作答')
    : nextId
      ? h('a', { class: 'btn primary', href: '#/class/' + classData.id + '/check/' + nextId }, '查看待核对作答')
      : h('a', { class: 'btn primary', href: '#/class/' + classData.id + '/check/records' }, '查看核对记录');
  const recordsLink = h('a', { class: 'btn', href: '#/class/' + classData.id + '/check/records' }, '查看核对记录');

  // R1-T12：批次选择。总览页原来只能靠作业表点进去，没有「当前看的是哪一批」的显式选择；
  // 这里补一个下拉，并把「当前作业 / 全部批次 / 具体批次」三种范围的差别写成一句话。
  const batchOptions = [
    { value: '', label: '当前作业（最近一次导入的真实批次）' },
    { value: CHECK_ALL_BATCHES, label: '全部批次（各批次并列相加）' },
  ].concat(assignments.map((item) => ({
    value: item.batch || '',
    label: (item.name || item.batch || '（未命名作业）') + (item.synthetic ? '（演示）' : ''),
  })));
  const batchSelect = h('select', { class: 'batch-select' },
    batchOptions.map((opt) => h('option', { value: opt.value }, opt.label)));
  batchSelect.value = viewAll ? CHECK_ALL_BATCHES : batch;
  batchSelect.addEventListener('change', () => {
    const value = batchSelect.value;
    location.hash = '#/class/' + classData.id + '/check'
      + (value ? '?batch=' + encodeURIComponent(value) : '');
  });
  const batchRow = h('div', { class: 'card batch-bar' },
    h('div', { class: 'row batch-row' },
      h('label', { class: 'small' }, '看哪个批次：'),
      batchSelect,
      h('span', { class: 'tiny muted batch-note' }, viewAll
        ? '当前范围：全部批次（' + assignments.length + ' 个作业，各批次并列相加；每份作答仍属于某个批次）'
        : (batch
          ? '当前范围：只看「' + assignmentName + '」这一批'
          : '当前范围：当前作业（最近一次导入的真实批次）；要并看所有批次请选「全部批次」'))),
    h('p', { class: 'tiny muted', style: 'margin:6px 0 0' },
      '「当前作业」只算最近一次导入的真实批次，「全部批次」把本班每个作业的应交/已核对/待核对并列相加，'
      + '两者分母不同；具体某一批请直接选批次名，或在下面作业表点那一行。'),
  );

  const hero = h('div', { class: 'card check-hero' },
    h('div', { class: 'row between' }, flag, showAssignment ? h('span', { class: 'rec-no' }, '当前作业：' + assignmentName) : null),
    h('h2', { class: 'state-title' }, headline),
    h('p', { class: 'small muted' }, detail),
    viewAll
      ? h('div', { class: 'kv-row' },
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '作业数'), h('div', { class: 'v' }, assignments.length)),
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '应交（各批合计）'), h('div', { class: 'v' }, allTotal)),
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '已核对（合计）'), h('div', { class: 'v' }, allChecked)),
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '待核对作答（合计）'), h('div', { class: 'v' }, allPending)))
      : null,
    !viewAll && overviewState === 'has_pending'
      ? h('div', { class: 'kv-row' },
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '应交'), h('div', { class: 'v' }, total)),
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '已核对'), h('div', { class: 'v' }, checked)),
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '待核对作答'), h('div', { class: 'v' }, pending)),
        h('div', { class: 'kv' }, h('div', { class: 'k' }, '缺测'), h('div', { class: 'v' }, missing)),
      )
      : null,
    h('div', { class: 'check-actions' }, primary,
      overviewState === 'all_checked' ? null : recordsLink),
  );

  // A7：合成演示必须有界面入口。归零/载入都只动合成数据，成功后立刻刷新本页数据。
  let demoBusy = false;
  const demoFeedback = h('div', { class: 'banner danger', style: 'margin-top:10px' }, '');
  demoFeedback.hidden = true;
  const demoNotice = state.ui.demoNotice || '';
  state.ui.demoNotice = '';
  const seedBtn = h('button', { class: 'btn', onclick: () => runDemo('seed') }, '载入合成演示数据');
  const resetBtn = h('button', { class: 'btn', onclick: () => runDemo('reset') }, '重置合成数据');
  const demoCard = h('div', { class: 'card demo-card' },
    h('h3', {}, '合成演示数据'),
    h('p', { class: 'small muted' },
      '演示不需要选文件、配模型或等网络：载入后到上方作业表点「合成演示」那一行即可开始'
      + '（演示批次单独成一个作业，不会顶替当前的真实作业）。'),
    demoNotice ? h('div', { class: 'banner info' }, demoNotice) : null,
    h('div', { class: 'check-actions', style: 'margin-top:8px' },
      seedBtn, resetBtn, h('span', { class: 'tiny muted' }, '只影响合成数据，真实导入的作答不受影响')),
    demoFeedback,
  );

  async function runDemo(action) {
    if (demoBusy) return;
    demoBusy = true;
    demoFeedback.hidden = true;
    seedBtn.disabled = true;
    resetBtn.disabled = true;
    try {
      const result = await api('/api/classes/' + classData.id + '/check/demo/' + action, {
        method: 'POST', body: {},
      });
      if (action === 'seed') {
        const seeded = result.seeded || {};
        state.ui.demoNotice = '已载入合成演示数据：新增 ' + checkNum(seeded.answers) + ' 份'
          + (checkNum(seeded.skipped_existing) ? '，跳过已存在的 ' + checkNum(seeded.skipped_existing) + ' 份' : '')
          + '。只影响合成数据。';
      } else {
        const deleted = result.deleted || {};
        const kept = result.kept || {};
        state.ui.demoNotice = '已重置合成数据：删除 ' + checkNum(deleted.answers) + ' 份作答、'
          + checkNum(deleted.followups) + ' 条追问；真实导入的作答保留 ' + checkNum(kept.imported_answers) + ' 份。';
      }
      await renderRoute();
    } catch (err) {
      demoFeedback.hidden = false;
      demoFeedback.textContent = (action === 'seed' ? '载入合成演示数据失败：' : '重置合成数据失败：')
        + (err.message || '请稍后重试') + '。';
    } finally {
      demoBusy = false;
      seedBtn.disabled = false;
      resetBtn.disabled = false;
    }
  }

  const assignmentCard = (assignments.length && (overviewState !== 'no_answers' || viewAll))
    ? h('div', { class: 'card' },
      h('h3', {}, '作业与待核对作答数量'),
      h('div', { class: 'table-wrap' },
        h('table', {},
          h('thead', {}, h('tr', {},
            h('th', {}, '作业'), h('th', {}, '应交'), h('th', {}, '已核对'), h('th', {}, '待核对作答'), h('th', {}, ''))),
          h('tbody', {}, assignments.map((item) => {
            const isCurrent = (item.batch || '') === (assignment.batch || batch || '');
            return h('tr', {
              class: 'clickable',
              onclick: () => { location.hash = '#/class/' + classData.id + '/check?batch=' + encodeURIComponent(item.batch || ''); },
            },
            h('td', {}, item.name || item.batch || '（未命名作业）',
              item.synthetic ? h('span', { class: 'chip mock', style: 'margin-left:6px' }, '演示') : null),
            h('td', { class: 'num' }, checkNum(item.total)),
            h('td', { class: 'num' }, checkNum(item.checked)),
            h('td', { class: 'num' }, checkNum(item.pending)),
            h('td', {}, isCurrent ? h('span', { class: 'chip ok' }, '当前') : null));
          })))) )
    : null;

  // A1：新建班级、AI 设置降为次级入口；按钮保留边框，避免看起来像一行说明文字。
  const secondary = h('div', { class: 'card secondary-card' },
    h('h3', {}, '其他入口'),
    h('div', { class: 'row' },
      h('button', { class: 'btn sm', onclick: promptCreateClass }, '新建班级'),
      h('a', { class: 'btn sm', href: '#/settings' }, 'AI 设置'),
      overviewState === 'no_answers' ? null : h('a', { class: 'btn sm', href: '#/upload?class=' + classData.id }, '导入作答'),
      h('button', { class: 'btn sm', onclick: () => renderRoute() }, '刷新'),
    ),
    h('p', { class: 'tiny muted' }, '以上是次级入口；本页主操作只有一个，按当前状态给出（导入作答、查看待核对作答或查看核对记录）。'),
  );

  return h('div', { class: 'check-overview' },
    h('p', { class: 'small muted', style: 'margin:0 0 8px' }, '课前总览 · 先看这批作答里哪一份需要问一句'),
    batchRow,
    hero,
    demoCard,
    assignmentCard,
    secondary,
  );
}

/* ---------------- A2/A3/A4/A5：作答核对 ---------------- */
async function renderCheckAnswer(classData, answerId) {
  const base = '/api/classes/' + classData.id + '/check';
  const data = (await api(base + '/answers/' + answerId)) || {};
  const answer = data.answer || {};
  const loc = data.locate || {};
  const judgement = data.judgement || {};
  const followup = data.followup || {};
  const queue = data.queue || {};
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  const draft = checkDraft(answerId);
  const batch = answer.batch || queue.batch || '';
  // R1-06：建议问句与「有没有依据」都以服务端为准（followup.suggestion 由后端按本条错步生成）。
  const followSuggestion = followup.suggestion || {};
  const askAvailable = followup.available === undefined
    ? Boolean(followup.question) : followup.available === true;
  const askQuestion = askAvailable ? (followup.question || followSuggestion.question || '') : '';
  const gateBad = (loc.status || '') === 'first_bad';
  const badIndex = loc.first_bad_index === null || loc.first_bad_index === undefined
    ? null : Number(loc.first_bad_index);

  const wrapper = h('div', { class: 'check-answer' });

  const queueLine = (queue.position || queue.total)
    ? '队列第 ' + checkNum(queue.position) + ' / ' + checkNum(queue.total) + ' 份' + (queue.is_last ? '（最后一份）' : '')
    : '';

  const head = h('div', { class: 'check-head' },
    h('div', {},
      h('div', { class: 'small' }, h('a', { href: '#/class/' + classData.id + '/check' }, '← 返回作答核对')),
      h('h3', { class: 'answer-title' },
        answer.student_label || '学生',
        h('span', { class: 'small muted' }, ' · 第 ' + (answer.question_no || '—') + ' 题'),
        h('span', { class: 'rec-no' }, '记录编号 ' + (answer.record_key || ('#' + (answer.id || answerId)))),
      ),
    ),
    h('div', { class: 'row' },
      h('span', { class: 'chip' + (answer.data_source === 'synthetic' ? ' mock' : '') },
        answer.data_source_label || (answer.data_source === 'synthetic' ? '合成演示数据' : '真实导入')),
      h('span', { class: 'rec-no' }, queueLine),
    ),
  );

  /* ---- 左列：题目、逐行原文、对照、标准解 ---- */
  const questionCard = h('div', { class: 'card' },
    h('h3', {}, '题目'),
    h('p', { class: 'step-math' }, answer.question_text || '（这份作答没有题目文本）'),
    h('p', { class: 'small muted' },
      [answer.unit, batch, answer.occurred_on ? '作答日期 ' + fmtDate(answer.occurred_on) : ''].filter(Boolean).join(' · ')),
    answer.missing
      ? h('div', { class: 'banner warn' }, '缺测：' + (answer.missing_reason || '这份作答没有可核验的步骤。'))
      : null,
  );

  const lines = checkLines(loc, answer);
  const lineNodes = lines.map((line, index) => checkLineRow(
    line, index, loc,
    gateBad && (line.bad === true || (badIndex !== null && index === badIndex)),
  ));
  const linesBody = lineNodes.length > CHECK_LONG_LINES
    ? h('div', {},
      h('div', { class: 'step-list' }, lineNodes.slice(0, CHECK_LONG_LINES)),
      h('details', { class: 'more-steps' },
        h('summary', {}, '展开其余 ' + (lineNodes.length - CHECK_LONG_LINES) + ' 行（长作答不横向滚动）'),
        h('div', { class: 'step-list' }, lineNodes.slice(CHECK_LONG_LINES)),
      ))
    : h('div', { class: 'step-list' }, lineNodes);

  const stepsCard = h('div', { class: 'card' },
    // 「首处不等价」只在错步行里出现一次；标题旁再放一枚会让人以为它在标记整段。
    h('h3', {}, '学生原文（逐行）'),
    lines.length ? linesBody : h('p', { class: 'small muted' }, '这份作答没有正文步骤。'),
    checkLocateNotice(loc, data.notice),
    gateBad ? checkContrast(loc) : null,
    gateBad && loc.reason_note ? h('p', { class: 'small muted', style: 'margin-top:8px' }, loc.reason_note) : null,
    loc.source === 'preset' ? h('div', { class: 'banner warn', style: 'margin-top:10px' }, CHECK_PRESET_NOTE) : null,
    checkStdSolution(answer),
  );

  /* ---- 右列：三个区块 ---- */
  const initialObserved = draft.observed !== undefined
    ? draft.observed
    : (judgement.observed_step || loc.first_bad_text || '');
  const observedInput = h('input', { type: 'text', class: 'observed-input', value: initialObserved });
  observedInput.addEventListener('input', () => { draft.observed = observedInput.value; });

  const stepBlock = h('div', { class: 'card' },
    h('h3', {}, '观察到的错步'),
    h('label', { class: 'field' },
      h('span', { class: 'lab' }, '学生原文里的这一处（老师可改）'),
      observedInput,
    ),
    h('p', { class: 'tiny muted' }, gateBad
      ? '只记录观察到的不等价，不推断学生为什么这样写。'
      // R1-05：规则判不了的作答（超出核验范围 / 缺步骤）也有人工核验出路——
      // 自己写下观察到的错误步骤 + 判断依据，服务端按「人工核验」单独留痕，不冒充规则定位。
      : '本次定位没有给出错步行：可以留空，也可以手动写下你观察到的错误步骤；'
        + '手写错步算「人工核验」，需要同时在「教师判断」卡里填判断依据。'),
  );

  /* ---- R1-T12 流程③：补录 / 校正原始作答（复用现有 field/textarea/btn 组件，不新增视觉） ---- */
  const editSteps = h('textarea', {
    rows: 6,
    placeholder: '逐行照抄学生原文，一行一步；补上缺的中间步骤后保存即可',
  });
  editSteps.value = draft.editSteps !== undefined ? draft.editSteps : (answer.student_steps || '');
  editSteps.addEventListener('input', () => { draft.editSteps = editSteps.value; });
  const editMissing = h('input', { type: 'checkbox' });
  editMissing.checked = draft.editMissing !== undefined ? !!draft.editMissing : !!Number(answer.missing || 0);
  editMissing.addEventListener('change', () => { draft.editMissing = editMissing.checked; });
  const editReason = h('input', {
    type: 'text', placeholder: '缺测原因（标为缺测时填，例如「未提供学生步骤」）',
    value: answer.missing_reason || '',
  });
  // 注意：这里**不能**用 `.banner.danger`。它排在页面底部操作栏之前，而保存失败横幅
  // （fillError）用的是同一个类；多一个空的 `.banner.danger` 会让既有断言读到空文本
  // （实测把 13.10 的 500 横幅两条断言带红）。用独立的轻量提示元素。
  const editError = h('p', { class: 'small check-edit-error', style: 'margin:8px 0 0; color:#b91c1c' }, '');
  editError.hidden = true;
  const editBtn = h('button', { class: 'btn', onclick: () => submitContent() }, '保存补录并重新分析');
  async function submitContent() {
    editError.hidden = true;
    editBtn.disabled = true;
    try {
      const payload = {
        expected_version: answer.version || '',
        student_steps: String(editSteps.value || ''),
        missing: editMissing.checked ? '是' : '否',
      };
      if (editMissing.checked) payload.missing_reason = String(editReason.value || '').trim();
      const result = await api(base + '/answers/' + answerId + '/content',
        { method: 'PATCH', body: payload }) || {};
      const prev = String(result.previous_version || '').slice(0, 8);
      const next = String(result.version || '').slice(0, 8);
      // 回执只说服务端的话：新版本、旧版本作废、是否演示预置失效（演示作答校正后由后端标注）。
      state.ui.checkNotice = {
        answerId: answerId,
        text: '已按新内容重新分析：旧版本 ' + (prev || '—') + ' 作废、新版本 ' + (next || '—')
          + '；这份作答已回到待核对。' + (result.note ? ' ' + result.note : ''),
      };
      state.ui.checkDrafts[String(answerId)] = {};
      await renderRoute();
    } catch (err) {
      editError.hidden = false;
      editError.textContent = '补录没有保存：' + (err.message || '请稍后重试')
        + '。（本次没有改动这份作答）';
    } finally {
      editBtn.disabled = false;
    }
  }
  const editCard = h('details', { class: 'card check-edit' },
    h('summary', {}, '补录 / 校正原始作答'),
    h('p', { class: 'small muted' },
      '缺步骤、步骤写错、学生后来又补了过程——都在这里改，不用重新导入整批。'
      + '保存后生成新版本：旧结论作废、本份回到待核对，并按新内容重新分析。'),
    h('label', { class: 'field' },
      h('span', { class: 'lab' }, '学生步骤（逐行原文）'),
      editSteps,
    ),
    h('label', { class: 'row', style: 'margin:6px 0' },
      editMissing, h('span', { class: 'small' }, '标为缺测（这份没有学生步骤）')),
    h('label', { class: 'field' },
      h('span', { class: 'lab' }, '缺测原因'),
      editReason,
    ),
    h('div', { class: 'check-actions', style: 'margin-top:6px' }, editBtn),
    editError,
  );

  /* ---- R1-T12 流程②：规则判不了时的人工核对入口（点一下就把人工判断区展开并聚焦） ---- */
  const manualCheckEntry = (loc.status === 'out_of_scope')
    ? h('div', { class: 'card manual-check-entry' },
      h('h3', {}, '规则判不了这份作答'),
      h('p', { class: 'small muted' },
        '可以选择「人工核对并记录错误」：写下你观察到的错误步骤、原因与判断依据，'
        + '保存后这份不再挂在「原因待判断」上（依据为空会被服务端拒绝并提示）。'),
      h('div', { class: 'check-actions' },
        h('button', {
          class: 'btn',
          onclick: () => {
            manualWrap.open = true;
            if (observedInput) observedInput.focus();
            basisArea.focus();
            toast('已展开人工核对：请填观察到的错误步骤、原因与判断依据');
          },
        }, '人工核对并记录错误')),
    )
    : null;

  /* ---- R1-T12：补录成功后的页内回执（只从服务端拿版本号与说明） ---- */
  const notice = state.ui.checkNotice && state.ui.checkNotice.answerId === answerId
    ? state.ui.checkNotice : null;
  if (notice) state.ui.checkNotice = null;
  const noticeBanner = notice
    ? h('div', { class: 'banner info' }, notice.text)
    : null;

  const selected = { code: draft.causeCode !== undefined ? draft.causeCode : (judgement.cause_code || '') };
  let adoptBtn = null;
  const syncAdopt = () => {
    if (!adoptBtn) return;
    adoptBtn.disabled = !selected.code || !candidates.some((item) => (item.code || '') === selected.code);
  };

  const causeRows = candidates.map((candidate) => {
    const code = candidate.code || '';
    const radio = h('input', { type: 'radio', name: 'check-cause-' + answerId, value: code, checked: selected.code === code });
    radio.addEventListener('change', () => {
      if (!radio.checked) return;
      selected.code = code;
      draft.causeCode = code;
      syncAdopt();
    });
    return h('label', { class: 'cause-item' + (candidate.selected ? ' preset-selected' : '') },
      h('span', { class: 'cause-pick' }, radio),
      h('span', { class: 'cause-body' },
        h('span', { class: 'cause-title' }, candidate.label || code || '（未命名候选）'),
        code ? h('span', { class: 'rec-no' }, code) : null,
        checkEvidenceList('支持', candidate.support),
        checkEvidenceList('削弱', candidate.rebut),
        checkEvidenceList('待补', candidate.pending),
      ),
    );
  });

  // A6：每屏只有一个 .btn.primary——「采纳原因」是次级动作，不抢主按钮层级。
  adoptBtn = h('button', {
    class: 'btn',
    disabled: true,
    onclick: () => {
      const candidate = candidates.find((item) => (item.code || '') === selected.code);
      save(candidate);
    },
  }, '采纳选中原因并保存');

  // A6：采纳是次级动作，跟候选放在一起：滚到候选就能看见，不用再回到页面底部找第二个提交按钮。
  const adoptRow = causeRows.length
    ? h('div', { class: 'adopt-row' },
      adoptBtn,
      h('span', { class: 'tiny muted' },
        '选中上面一条候选后，可直接采纳并保存（原因记为「教师已判断」）；不采纳就点底部「保存错步，原因待判断」。'))
    : null;

  // R1-02：没有候选也要能完成判断——人工填写原因编码与说明，来源记 teacher（与采纳候选可区分）。
  const manualCode = h('input', {
    type: 'text', class: 'manual-code', placeholder: '原因编码（可留空）',
    value: draft.manualCode !== undefined ? draft.manualCode : (judgement.cause_code || ''),
  });
  manualCode.addEventListener('input', () => { draft.manualCode = manualCode.value; });
  const manualLabel = h('input', {
    type: 'text', class: 'manual-label', placeholder: '原因说明（必填，例如「移项没变号」）',
    value: draft.manualLabel !== undefined ? draft.manualLabel : (judgement.cause_label_text || ''),
  });
  manualLabel.addEventListener('input', () => {
    draft.manualLabel = manualLabel.value;
    if (manualBtn) manualBtn.disabled = !String(manualLabel.value || '').trim();
  });
  const manualBtn = h('button', {
    class: 'btn',
    disabled: !String(manualLabel.value || '').trim(),
    onclick: () => saveManual(),
  }, '保存人工判断');
  const manualWrap = h('details', { class: 'manual-cause', open: !causeRows.length },
    h('summary', {}, '没有合适候选？人工填写原因（来源记为「教师填写」）'),
    h('div', { class: 'manual-row' }, manualCode, manualLabel),
    h('div', { class: 'check-actions', style: 'margin-top:6px' }, manualBtn),
    h('p', { class: 'tiny muted' },
      '人工判断与采纳候选都能完成「教师已判断」，但来源不同（教师填写 / 采纳候选），'
      + '记录里可以区分。判断依据请填上方「教师判断」卡里的「判断依据」。'),
  );

  const causeBlock = h('div', { class: 'card' },
    h('h3', {}, '可能原因'),
    causeRows.length
      ? h('div', { class: 'cause-list' }, causeRows)
      : h('p', { class: 'small muted' }, data.causes_note || '尚无已审错因库可用，保留待判断。'),
    adoptRow,
    manualWrap,
    data.causes_note ? h('p', { class: 'tiny muted' }, data.causes_note) : null,
  );
  const noteArea = h('textarea', { rows: 2, placeholder: '可选：记一句备注，例如「让他重做一道 5(y-3)」' });
  noteArea.value = draft.note !== undefined ? draft.note : (judgement.teacher_note || '');
  noteArea.addEventListener('input', () => { draft.note = noteArea.value; });

  // R1-02：判断依据与备注分列——依据进 basis_note（谁凭什么这么判），备注进 teacher_note。
  // T11：规则判不了的作答（applicability=optional，如超出核验范围）人工改判原因时，
  // 后端强制要求依据（400 basis_required）。这里只加一句必填提示，不改布局；
  // 已经记成「无需判断」的不再提示（老师并没有形成原因判断）。
  const basisRequired = (data.applicability || '') === 'optional'
    && (judgement.cause_state || '') !== 'not_applicable';
  const basisArea = h('textarea', {
    rows: 2,
    placeholder: basisRequired
      ? '必填：本题规则判不了，写清你凭哪条证据判的原因（例如「作业本原图第 2 行写的是 2x>2，他抄成 x<1」）'
      : '可选：这次判断的依据，例如「第 2 行把 −6 写成 −2，课堂口述也这么说」',
  });
  basisArea.value = draft.basis !== undefined ? draft.basis : (judgement.basis_note || '');
  basisArea.addEventListener('input', () => { draft.basis = basisArea.value; });
  const basisHint = basisRequired
    ? h('p', { class: 'tiny muted basis-hint' },
      '本题超出规则核验范围：要判原因就必须写判断依据，否则保存会被拒绝（basis_required）；'
      + '确认无需判断就直接点主按钮，系统会记成「无需判断」，不要求依据。')
    : null;

  const errorBox = h('div', { class: 'banner danger', style: 'margin:0 0 6px' }, '');
  errorBox.hidden = true;
  const refreshRow = h('div', { class: 'tiny', style: 'margin-top:6px' },
    h('a', { href: '#/class/' + classData.id + '/check/' + answerId }, '刷新这份作答（会清空本页输入）'));
  refreshRow.hidden = true;

  // R-04（契约 §4.2）：这份作答已经存过「教师已判断」的原因时，主按钮不能再写「原因待判断」——
  // 那会让按钮说谎。改按状态换文案，并补一条「改为原因待判断」的次级路径，
  // 让 A5 要求的「保留待判断」在改判之后仍然可达。
  // R1-02：人工判断允许只填说明不填编码，所以这里按状态判断，不再要求 code 非空。
  const decidedCause = judgement.cause_state === 'decided';
  const SAVE_LABEL_PENDING = '保存错步，原因待判断';
  const SAVE_LABEL_DECIDED = '保存错步（沿用已判断原因）';
  const saveLabel = () => (decidedCause ? SAVE_LABEL_DECIDED : SAVE_LABEL_PENDING);

  const saveBtn = h('button', { class: 'btn primary', onclick: () => save(null) }, saveLabel());
  const resetCauseBtn = decidedCause
    ? h('button', { class: 'btn sm', onclick: () => save(null, { undecided: true }) }, '改为原因待判断')
    : null;
  // R1-02「修改或撤回」：撤回=把当前版本的结论作废，本份回到待核对队列（历史留痕，不删行）。
  const canRetract = judgement.progress === 'checked';
  const retractBtn = canRetract
    ? h('button', { class: 'btn sm', onclick: () => save(null, { retract: true }) },
      '撤回本次结论（回到待核对）')
    : null;
  const retractedLine = judgement.retracted && judgement.retracted.at
    ? h('p', { class: 'tiny muted' },
      '上一次结论已于 ' + fmtDateTime(judgement.retracted.at) + ' 撤回'
      + (judgement.retracted.reason ? '：' + judgement.retracted.reason : '')
      + '。结论已作废，本份回到待核对队列。')
    : null;

  const judgeBlock = h('div', { class: 'card judge-card' },
    h('h3', {}, '教师判断'),
    judgement.stale
      ? h('div', { class: 'banner warn', style: 'margin-top:8px' }, '作答已更新，旧判断不再作为当前结论。')
      : null,
    retractedLine,
    decidedCause
      ? h('div', { class: 'judge-current' },
        h('p', { style: 'margin:0' }, '当前原因：', h('b', {}, judgement.cause_label_text || judgement.cause_code || '')),
        checkCauseSourceText(judgement)
          ? h('p', { class: 'tiny muted', style: 'margin:4px 0 0' }, checkCauseSourceText(judgement))
          : null,
        h('div', { class: 'check-actions', style: 'margin-top:8px' }, resetCauseBtn),
        h('p', { class: 'tiny muted' },
          '主按钮沿用这条原因；点「改为原因待判断」会把旧原因记入改判历史，原因回到待判断。'))
      : null,
    h('label', { class: 'field', style: 'margin-top:8px' },
      h('span', { class: 'lab' }, basisRequired
        ? '判断依据（本题规则判不了：人工改判原因时必填）'
        : '判断依据（可选，与备注分开记录）'),
      basisArea,
      basisHint,
    ),
    h('label', { class: 'field', style: 'margin-top:8px' },
      h('span', { class: 'lab' }, '教师备注（可选，随这次保存一起记录）'),
      noteArea,
    ),
    retractBtn
      ? h('div', { class: 'check-actions', style: 'margin-top:8px' }, retractBtn)
      : null,
    retractBtn
      ? h('p', { class: 'tiny muted' }, '撤回不会删除记录：旧结论进历史，本份回到「待核对」，可以重新核对。')
      : null,
    h('p', { class: 'tiny muted' }, CHECK_QUEUE_NOTE),
  );
  syncAdopt();

  // A6：底部操作栏只留一个提交动作（保存错步，原因待判断）；采纳动作在候选区里。
  // 主按钮固定在视口底部操作栏里，1366×768 首屏就能看到错步和主动作。
  const actionBar = h('div', { class: 'check-bar' },
    h('div', { class: 'check-bar-inner' },
      h('div', { class: 'check-bar-row' },
        checkAxisChips(judgement),
        h('span', { class: 'tiny muted' }, decidedCause
          ? '当前是「教师已判断」：主按钮沿用这条原因；改判请在候选区点「采纳选中原因并保存」。'
          : '原因留空即「原因待判断」，课堂上可以再补记；保存后退出待核对队列。'),
      ),
      errorBox,
      refreshRow,
      h('div', { class: 'check-actions', style: 'margin-top:0' }, saveBtn),
    ),
  );

  /* ---- A4：当面追问（R1-05/R1-06）---- */
  let skipped = draft.skipped === true;
  // R1-06：问句可以改——线下提问就写老师自己实际问的那一句，服务端按「是否逐字等于本条建议
  // 问句」判来源（preset / teacher / library），前端不再自己编问句、也不再用写死的常量兜底。
  const askInput = h('input', { type: 'text', class: 'ask-input' });
  askInput.value = draft.askQuestion !== undefined ? draft.askQuestion : askQuestion;
  askInput.addEventListener('input', () => { draft.askQuestion = askInput.value; });
  const askNow = () => String(askInput.value || '').trim();

  const followArea = h('textarea', { rows: 2, placeholder: '逐字记下学生原话，例如：2 只要乘 y 就好了。' });
  followArea.value = draft.followup !== undefined
    ? draft.followup
    : (followup.recorded ? (followup.answer_text || '') : '');
  const followWrap = h('div', { class: 'follow-wrap' },
    h('label', { class: 'field' },
      h('span', { class: 'lab' }, '学生原话（逐字记录，跟随本页保存）'),
      followArea,
    ),
    h('p', { class: 'tiny muted' }, '「暂时跳过」只跳过提问：不发请求、不跳页、不保存，也不等于学生拒答。'),
  );
  followWrap.hidden = !(followup.recorded || String(followArea.value || '').length);
  followArea.addEventListener('input', () => {
    draft.followup = followArea.value;
    draft.skipped = false;
    skipped = false;
    skipLine.hidden = true;
  });

  const skipLine = h('p', { class: 'tiny muted' }, skipped ? '已跳过提问，未记录回答' : '');
  skipLine.hidden = !skipped;

  const recordBtn = h('button', {
    class: 'btn',
    onclick: () => {
      skipped = false;
      draft.skipped = false;
      skipLine.hidden = true;
      followWrap.hidden = false;
      followArea.focus();
    },
  }, '记录学生回答');

  const skipBtn = h('button', {
    class: 'btn ghost',
    onclick: () => {
      // 本地状态而已：不 fetch、不动 hash、不写库（A4）；跳过不产生任何追问记录。
      skipped = true;
      draft.skipped = true;
      draft.followup = '';
      followArea.value = '';
      followWrap.hidden = true;
      skipLine.hidden = false;
      skipLine.textContent = '已跳过提问，未记录回答';
      toast('已跳过提问，未记录回答');
    },
  }, '暂时跳过');

  // R1-06 第 5 条：证据变了（改了错步/原因，或作答被补录校正），上一次的原话不再算已确认。
  const reviewBanner = followup.needs_review
    ? h('div', { class: 'banner warn', style: 'margin:8px 0' },
      h('b', {}, '上一次记录的学生回答已不再是当前证据'),
      h('p', { class: 'tiny', style: 'margin:4px 0 0' },
        followup.review_note || '这条追问依据的错步或原因已经变了，请重新确认。'))
    : null;

  const recordedLine = followup.recorded
    ? h('div', { class: 'quote-box' },
      h('div', { class: 'rec-no' },
        '上次记录的学生原话'
        + (followup.source_label ? '（问句来源：' + followup.source_label + '）' : '')),
      h('p', { class: 'quote' }, followup.answer_text || '（原话为空）'))
    : null;

  // 失效的历史原话仍然要看得到（「留历史」），但明确标成已失效、不是当前证据。
  const historyNodes = (followup.history || [])
    .filter((item) => item.answer_text && !item.current_version)
    .slice(0, 3)
    .map((item) => h('div', { class: 'quote-box' },
      h('div', { class: 'rec-no' },
        '旧版本记录（' + (item.invalidated_reason || '已失效') + '）'),
      h('p', { class: 'quote' },
        (item.question ? '问：' + item.question + '；' : '') + '学生原话：「' + item.answer_text + '」')));

  const askLine = askAvailable
    ? h('p', { class: 'ask-line' }, '可以这样问学生：', h('b', {}, askQuestion))
    : h('p', { class: 'ask-line' },
      h('b', {}, CHECK_ASK_UNAVAILABLE),
      h('span', { class: 'tiny muted' },
        ' ' + (followSuggestion.note || followup.note || '这一份没有可追问的错步。')));

  const askField = h('label', { class: 'field', style: 'margin-top:8px' },
    h('span', { class: 'lab' }, askAvailable
      ? '要问的那一句（可改；改了就算教师自拟）'
      : '线下提问：写下你实际问的那一句（选了「记录学生回答」就必须填）'),
    askInput,
  );

  const askCard = h('div', { class: 'card check-ask' },
    h('h3', {}, '当面追问'),
    askLine,
    askField,
    reviewBanner,
    recordedLine,
    historyNodes.length ? h('div', {}, historyNodes) : null,
    h('p', { class: 'tiny muted' }, '跳过不等于学生拒答；没有新增依据时，原因仍然待判断。'),
    h('div', { class: 'check-actions' }, recordBtn, skipBtn),
    skipLine,
    followWrap,
  );

  /* ---- 保存与回执（A5） ---- */
  function setBusy(busy) {
    saveBtn.disabled = busy;
    adoptBtn.disabled = busy || !candidates.some((item) => (item.code || '') === selected.code);
    if (resetCauseBtn) resetCauseBtn.disabled = busy;
    if (retractBtn) retractBtn.disabled = busy;
    // 忙完要回到本状态该有的文案，别把「沿用已判断原因」洗成「原因待判断」。
    saveBtn.textContent = busy ? '保存中…' : saveLabel();
  }

  function fillError(err) {
    const status = err && err.status;
    errorBox.hidden = false;
    refreshRow.hidden = status !== 409;
    if (status === 409) {
      errorBox.textContent = '这份作答已被更新（版本不一致），本次没有保存。'
        + '你填写的内容仍在本页；可以先复制下来，再点下面的「刷新这份作答」重新核对。';
    } else if (status === 400) {
      errorBox.textContent = '保存被拒绝：' + (err.message || '请求内容不完整') + '。改一下再点一次保存即可。';
    } else if (status) {
      errorBox.textContent = '保存失败（' + status + '）：' + (err.message || '服务器返回错误')
        + '。你填写的内容仍在页面上，可以再点一次「' + saveLabel() + '」重试。';
    } else {
      errorBox.textContent = '保存失败：' + (err.message || '网络连接失败')
        + '。你填写的内容仍在页面上，可以再点一次「' + saveLabel() + '」重试。';
    }
  }

  // 人工填写原因：没有候选也能完成判断（R1-02），来源固定标 teacher。
  async function saveManual() {
    const label = String(manualLabel.value || '').trim();
    if (!label) return;
    await save(null, { manual: true });
  }

  // adoptCandidate：采纳候选原因（改判）；options.undecided：显式回到「原因待判断」；
  // options.retract：撤回本次结论（progress=pending，服务端把当前版本结论作废）；
  // options.manual：人工填写原因（cause_source=teacher）。
  async function save(adoptCandidate, options) {
    errorBox.hidden = true;
    refreshRow.hidden = true;
    const forceUndecided = !!(options && options.undecided);
    const retract = !!(options && options.retract);
    const manual = !!(options && options.manual);
    const followed = skipped ? '' : String(followArea.value || '').trim();
    let causeState = 'undecided';
    let causeCode = '';
    let causeLabel = '';
    let causeSource = '';
    if (forceUndecided) {
      // 契约 §4.3：旧原因由后端推进 cause_history，请求体不再带 cause_code/cause_label。
      causeState = 'undecided';
    } else if (manual) {
      causeState = 'decided';
      causeCode = String(manualCode.value || '').trim();
      causeLabel = String(manualLabel.value || '').trim();
      causeSource = 'teacher';
    } else if (adoptCandidate) {
      causeState = 'decided';
      causeCode = adoptCandidate.code || '';
      causeLabel = adoptCandidate.label || '';
      causeSource = 'preset';
    } else if (decidedCause) {
      // 已经是「教师已判断」的：主按钮沿用原结论，不要在顺手保存时被打回待判断。
      // 来源让服务端自己判（编码仍在候选里就是 preset，否则 teacher），这里不硬塞。
      causeState = 'decided';
      causeCode = judgement.cause_code || '';
      causeLabel = judgement.cause_label_text || '';
    }
    const payload = {
      expected_version: answer.version || '',
      observed_step: String(observedInput.value || '').trim(),
      first_bad_index: badIndex === null || !Number.isInteger(badIndex) ? null : badIndex,
      cause_code: causeCode,
      cause_label: causeLabel,
      teacher_note: String(noteArea.value || '').trim(),
      basis_note: String(basisArea.value || '').trim(),
      cause_state: causeState,
      followup: { question: askNow(), answer_text: followed },
    };
    if (causeSource) payload.cause_source = causeSource;
    if (retract) {
      // 撤回只改进度轴：不提交任何原因字段，避免和后端的非法组合校验打架。
      payload.progress = 'pending';
      payload.change_reason = '老师在页面上撤回本次结论';
      delete payload.cause_code;
      delete payload.cause_label;
      delete payload.cause_state;
    }
    // R1-06：没有适用问句时也可以记线下提问，但必须写下实际问的那一句——
    // 本地先拦一次，别让老师等到服务端 400 才知道少了什么。
    if (!retract && followed && !askNow()) {
      errorBox.hidden = false;
      errorBox.textContent = '要记录学生回答，请先在「当面追问」里填上你实际问的那一句'
        + '（这份作答' + (askAvailable ? '' : '没有适用追问，') + '线下提问也可以）。本次没有保存。';
      return;
    }
    setBusy(true);
    try {
      const result = await api(base + '/answers/' + answerId + '/save', { method: 'POST', body: payload });
      state.ui.checkDrafts[String(answerId)] = {};
      // R1-03：回执只读服务端返回的权威值（不再拿请求里的 causeState/causeLabel 渲染）。
      showReceipt(result || {});
    } catch (err) {
      // A5：失败留在当前页，输入一个都不清。
      fillError(err);
    } finally {
      setBusy(false);
    }
  }

  // R1-03：回执**只读服务端响应**（result），不再用请求里的值兜底——
  // 否则 out_of_scope 点「采纳」时会出现「回执说教师已判断、chips 说原因待判断」的自相矛盾。
  function showReceipt(result) {
    const receipt = result.receipt || {};
    const after = result.judgement || {};
    const afterQueue = result.queue || {};
    const afterAnswer = result.answer || {};
    const afterFollowup = result.followup || {};
    const afterLocate = result.locate || {};
    const retracted = result.action === 'retracted';
    const className = receipt.class_name || classData.name || '本班级';
    const assignmentName = receipt.assignment_name || batch || '本次作业';
    const nextAfter = afterQueue.next_answer_id === undefined || afterQueue.next_answer_id === null
      ? null : Number(afterQueue.next_answer_id);
    const isLast = afterQueue.is_last === true || nextAfter === null;
    const remaining = afterQueue.remaining === undefined ? null : checkNum(afterQueue.remaining);
    const causeState = after.cause_state || 'undecided';
    const causeLabelText = causeState === 'decided'
      ? '教师已判断：' + (after.cause_label_text || after.cause_code || '（未命名原因）')
      : (causeState === 'not_applicable'
        ? '无需判断：本题没有可归因的错步（不计入已确认错因）'
        : '原因待判断（不计入已确认错因）');
    const observed = after.observed_step || afterLocate.first_bad_text || '';
    const followed = afterFollowup.recorded ? (afterFollowup.answer_text || '') : '';
    // 候选证据也用响应里的 candidates（保存响应带这一项），按响应里的 cause_source 判断是否已采纳。
    const afterCandidates = Array.isArray(result.candidates) ? result.candidates : [];
    const adopted = after.cause_source === 'preset';
    const evidence = afterCandidates.find((item) => (item.code || '') === (after.cause_code || ''))
      || (afterCandidates.length ? afterCandidates[0] : null);

    // R1-T12 流程④：依据一变，旧确认不再算「已记录」——回执必须当场说，别等老师刷新页面才发现。
    const reviewNode = afterFollowup.needs_review === true
      ? h('div', { class: 'banner warn', style: 'margin-top:8px' },
        h('b', {}, '依据已更新，需复核：'),
        afterFollowup.review_note
          || '这条追问依据的错步或原因已经变了，请重新确认后再当作当前证据。')
      : null;

    const quoteNode = followed
      ? h('div', { class: 'quote-box' },
        h('div', { class: 'rec-no' },
          '问：' + (afterFollowup.question || askNow() || CHECK_ASK_UNAVAILABLE)),
        h('p', { class: 'quote' }, '学生原话：「' + followed + '」'))
      : h('p', { class: 'small muted' }, '本次未记录学生回答（已跳过提问或未填写原话）；原因仍在待判断。');

    const evidenceNode = evidence
      ? h('div', { class: 'evidence' },
        h('div', { class: 'rec-no' },
          (adopted ? '已采纳的候选：' : '未采纳的候选依据：') + (evidence.label || evidence.code || '')),
        checkEvidenceList('支持', evidence.support),
        checkEvidenceList('削弱', evidence.rebut),
        checkEvidenceList('待补', evidence.pending))
      : h('p', { class: 'small muted' }, data.causes_note || '这份作答没有候选依据，原因继续待判断。');

    const primaryAction = isLast
      ? h('a', { class: 'btn primary', href: '#/class/' + classData.id + '/check' }, '返回课前总览')
      : h('a', { class: 'btn primary', href: '#/class/' + classData.id + '/check/' + nextAfter }, '查看下一份');

    const receiptNode = h('div', { class: 'receipt' },
      h('div', { class: 'card receipt-hero' },
        h('h2', {}, retracted ? '已撤回' : '已保存'),
        h('p', { class: 'receipt-line' },
          retracted ? '撤回后回到待核对：' : '已保存到：',
          h('b', {}, className), ' · ', h('b', {}, assignmentName)),
        h('p', { class: 'tiny muted' },
          (afterAnswer.student_label || '学生') + ' · 第 ' + (afterAnswer.question_no || '—') + ' 题'
          + ' · 记录编号 ' + (afterAnswer.record_key || '—')),
        receipt.status_line ? h('p', { class: 'small muted' }, '两轴状态：' + receipt.status_line) : null,
        checkAxisChips(after),
        receipt.saved_at ? h('p', { class: 'tiny muted' }, '保存时间：' + fmtDateTime(receipt.saved_at)) : null,
      ),
      h('div', { class: 'card' },
        h('h3', {}, '这次判断根据什么'),
        h('div', { class: 'kv' },
          h('div', { class: 'k' }, '观察到的错步'),
          h('p', { class: 'step-math' }, observed || '（未填写）')),
        // R1-05：人工核验与规则结果在回执里也分开说，别让老师以为都是规则给的。
        after.verify_source === 'manual'
          ? h('p', { class: 'small' }, '核验来源：教师人工核验'
            + (after.teacher_error_text ? '（人工记录的错步：「' + after.teacher_error_text + '」）' : ''))
          : (after.verify_source_label ? h('p', { class: 'small' }, '核验来源：' + after.verify_source_label) : null),
        quoteNode,
        reviewNode,
        evidenceNode,
        h('div', { class: 'kv', style: 'margin-top:10px' },
          h('div', { class: 'k' }, '教师判断'),
          h('p', {}, causeLabelText)),
        checkCauseSourceText(after)
          ? h('p', { class: 'small' }, checkCauseSourceText(after))
          : null,
        after.basis_note ? h('p', { class: 'small' }, '判断依据：' + after.basis_note) : null,
        after.teacher_note ? h('p', { class: 'small' }, '教师备注：' + after.teacher_note) : null,
      ),
      retracted
        ? h('div', { class: 'card' },
          h('div', { class: 'banner info' },
            '本次结论已作废，本份回到「待核对」队列；旧结论与撤回理由留在历史里，可以重新核对。'))
        : (isLast
          ? h('div', { class: 'card' },
            h('div', { class: 'banner info' },
              '本次作业的待核对作答已全部处理完。可以从「查看核对记录」找回原因待判断的记录，课堂上再补记学生原话。'))
          : h('div', { class: 'card' },
            h('p', { class: 'small', style: 'margin:0' }, remaining === null
              ? '同一个队列里还有待核对的作答。'
              : '这份已经保存，队列里还剩 ' + remaining + ' 份待核对（仍是本次作业）。'))),
      // A6：回执页的主动作（查看下一份）必须在首屏；放进固定操作栏，保存完不用滚动就能接着做。
      h('div', { class: 'check-bar' },
        h('div', { class: 'check-bar-inner' },
          h('div', { class: 'check-bar-row' },
            h('span', { class: 'small' }, isLast ? '本次作业已处理完' : '继续核对下一份'),
            h('span', { class: 'tiny muted' }, receipt.saved_at ? '保存于 ' + fmtDateTime(receipt.saved_at) : ''),
          ),
          h('div', { class: 'check-actions', style: 'margin-top:0' },
            primaryAction,
            h('a', { class: 'btn', href: '#/class/' + classData.id + '/check/records' }, '查看核对记录')),
        ),
      ),
    );
    // 保存成功的回执页不该再挂着上一屏「已跳过提问」之类的提示（走查发现 ⑤）。
    clearToast();
    wrapper.replaceChildren(receiptNode);
    window.scrollTo(0, 0);
  }

  const grid = h('div', { class: 'check-split' },
    h('div', { class: 'check-col' }, questionCard, stepsCard, manualCheckEntry, editCard),
    h('div', { class: 'check-col check-side' }, stepBlock, causeBlock, judgeBlock),
  );
  wrapper.appendChild(h('div', { class: 'check-body' }, head, noticeBanner, grid, askCard, actionBar));
  return wrapper;
}

/* ---------------- A5：记录找回 ---------------- */
async function renderCheckRecords(classData, query) {
  // R1-02 三态：原因待判断 / 教师已判断 / 无需判断（正确·缺测·缺少步骤收口到这里）。
  const allowed = ['undecided', 'decided', 'not_applicable', 'all'];
  const causeState = allowed.indexOf(query.cause_state) >= 0 ? query.cause_state : 'undecided';
  const params = new URLSearchParams({ cause_state: causeState });
  const recordsBatch = query.batch || '';
  if (recordsBatch) params.set('batch', recordsBatch);

  const [records, overview] = await Promise.all([
    api('/api/classes/' + classData.id + '/check/records?' + params.toString()),
    api('/api/classes/' + classData.id + '/check/overview').catch(() => null),
  ]);
  const items = Array.isArray((records || {}).items) ? records.items : [];
  const next = overview && overview.next ? overview.next : null;
  const nextId = next && next.answer_id !== undefined && next.answer_id !== null ? Number(next.answer_id) : null;

  // R1-T12 流程⑤：记录页也要能选批次。不传批次 = 全班所有批次（契约 §3.4），
  // 选具体批次 = 只看那批；两种范围的差别写成一句话，避免「为什么这里 8 条、统计页 3 条」。
  const batchOptions = [{ value: '', label: '全部批次（本班所有批次）' }]
    .concat(((overview || {}).assignments || []).map((item) => ({
      value: item.batch || '',
      label: (item.name || item.batch || '（未命名作业）') + (item.synthetic ? '（演示）' : ''),
    })));
  const batchSelect = h('select', { class: 'batch-select' },
    batchOptions.map((opt) => h('option', { value: opt.value }, opt.label)));
  batchSelect.value = recordsBatch;
  batchSelect.addEventListener('change', () => {
    const chosen = batchSelect.value;
    location.hash = '#/class/' + classData.id + '/check/records?cause_state=' + causeState
      + (chosen ? '&batch=' + encodeURIComponent(chosen) : '');
  });
  const batchBar = h('div', { class: 'row batch-row batch-bar', style: 'margin:0 0 8px' },
    h('label', { class: 'small' }, '看哪个批次：'),
    batchSelect,
    h('span', { class: 'tiny muted batch-note' }, recordsBatch
      ? '当前范围：只看「' + recordsBatch + '」这一批'
      : '当前范围：全部批次（本班所有批次，每行都标着所属批次）'));

  const filters = [
    ['undecided', '原因待判断'],
    ['decided', '教师已判断'],
    ['not_applicable', '无需判断'],
    ['all', '全部'],
  ];

  const filterRow = h('div', { class: 'row', style: 'margin-bottom:10px' },
    h('span', { class: 'small muted' }, '筛选：'),
    filters.map(([key, label]) => h('a', {
      class: 'chip' + (key === causeState ? ' ok' : ''),
      href: '#/class/' + classData.id + '/check/records?cause_state=' + key,
    }, label)),
  );

  const primary = nextId
    ? h('a', { class: 'btn primary', href: '#/class/' + classData.id + '/check/' + nextId }, '查看待核对作答')
    : h('button', { class: 'btn primary', disabled: true, title: '当前没有待核对的作答' }, '查看待核对作答');

  const body = items.length
    ? h('div', { class: 'table-wrap' },
      h('table', {},
        h('thead', {}, h('tr', {},
          h('th', {}, '学生'), h('th', {}, '题号'), h('th', { class: 'hide-sm' }, '作业批次'),
          h('th', {}, '观察到的错步'), h('th', {}, '处理进度'), h('th', {}, '原因判断'),
          h('th', { class: 'hide-sm' }, '保存时间'), h('th', {}, ''))),
        h('tbody', {}, items.map((item) => h('tr', {
          class: 'clickable',
          onclick: () => { location.hash = '#/class/' + classData.id + '/check/' + item.answer_id; },
        },
        h('td', {}, item.student_label || '—'),
        h('td', { class: 'num' }, item.question_no || '—'),
        h('td', { class: 'small muted hide-sm' }, item.batch || '—'),
        h('td', { class: 'step-math sm' }, item.observed_step || '—'),
        h('td', {}, h('span', { class: 'axis-chip progress' + (item.progress === 'checked' ? ' on' : '') },
          item.progress === 'checked' ? '已核对' : '待核对')),
        h('td', {}, h('span', {
          class: 'axis-chip cause' + (item.cause_state === 'decided' ? ' on' : '')
            + (item.cause_state === 'not_applicable' ? ' na' : ''),
        },
        item.cause_label_text || item.cause_state_label
          || (item.cause_state === 'decided' ? '教师已判断'
            : (item.cause_state === 'not_applicable' ? '无需判断' : '原因待判断')))),
        h('td', { class: 'small muted hide-sm' }, fmtDateTime(item.saved_at)),
        // 行内动作要有按钮形态（走查发现 ⑪）：纯文字看不出能点。
        h('td', {}, h('button', {
          class: 'btn sm',
          onclick: (event) => {
            event.stopPropagation();
            location.hash = '#/class/' + classData.id + '/check/' + item.answer_id;
          },
        }, '打开')),
        )))))
    : h('div', { class: 'card muted' },
      '这个筛选下还没有记录。',
      h('p', { class: 'small' }, '保存过的作答会出现在这里；按「原因待判断」能找回还没定原因的那几份。'));

  return h('div', { class: 'check-records' },
    h('p', { class: 'small muted', style: 'margin:0 0 8px' },
      h('a', { href: '#/class/' + classData.id + '/check' }, '← 返回作答核对'), ' · 核对记录'),
    h('div', { class: 'card' },
      h('div', { class: 'row between' },
        h('h3', {}, '核对记录（' + checkNum((records || {}).total) + ' 条）'),
        primary),
      h('p', { class: 'small muted' }, '只列当前版本的有效记录，按保存时间倒序。改判后的旧原因不再作为当前结论，但仍保留在历史里。'),
      batchBar,
      filterRow,
      body,
    ),
  );
}

/* ------------------------------------------------------------------ */
/* 路由                                                                */
/* ------------------------------------------------------------------ */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  return {
    seg: pathPart.split('/').filter(Boolean),
    query: Object.fromEntries(new URLSearchParams(queryPart || '')),
  };
}

let rendering = null;
let rerenderQueued = false;

async function renderRoute() {
  // 页面渲染串行：快速切换页面时，避免前一次异步加载晚到、把新页面覆盖掉。
  if (rendering) { rerenderQueued = true; return rendering; }
  rendering = doRender();
  try {
    await rendering;
  } finally {
    rendering = null;
  }
  if (rerenderQueued) {
    rerenderQueued = false;
    await renderRoute();
  }
}

async function doRender() {
  const { seg, query } = parseHash();
  const topbar = document.getElementById('topbar');
  // 换页时把上一屏的提示清掉：提示属于上一屏的动作结果，跟到回执页/记录页会误导（走查发现 ⑤）。
  clearToast();

  if (!state.user) {
    topbar.hidden = true;
    document.getElementById('who').textContent = '';
    if (seg[0] === 'register') { renderRegister(); return; }
    if (seg[0] !== 'login') { location.hash = '#/login'; return; }
    renderLogin();
    return;
  }
  topbar.hidden = false;
  await ensureMeta();
  updateAiBadge();
  document.getElementById('who').textContent = state.user.display_name || state.user.username;

  if (seg[0] === 'login' || seg[0] === 'register' || !seg.length) { location.hash = '#/classes'; return; }

  $app.replaceChildren(h('div', { class: 'muted' }, '加载中…'));
  try {
    if (seg[0] === 'classes') return await renderClasses();
    // 班级页：seg[2] 是 tab（默认「作答核对」），seg[3] 是 tab 内的子页面。
    if (seg[0] === 'class') return await renderClass(Number(seg[1]), seg[2] || 'check', seg[3] || '', query);
    if (seg[0] === 'mistake') return await renderMistake(Number(seg[1]));
    if (seg[0] === 'student') return await renderStudent(Number(seg[1]));
    if (seg[0] === 'upload') return await renderUpload(query);
    if (seg[0] === 'settings') return await renderSettings();
    location.hash = '#/classes';
  } catch (err) {
    $app.replaceChildren(h('div', { class: 'card' },
      h('h3', {}, '出错了'),
      h('p', { class: 'muted' }, err.message),
      h('a', { class: 'btn sm', href: '#/classes' }, '返回班级列表')));
  }
}

document.getElementById('logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (err) { /* 忽略 */ }
  state.user = null;
  location.hash = '#/login';
  renderRoute();
});

window.addEventListener('hashchange', renderRoute);

(async function boot() {
  try {
    const me = await api('/api/auth/me', { quiet: true });
    state.user = me.user;
    state.meta = await api('/api/meta', { quiet: true });
  } catch (err) {
    state.user = null;
  }
  await renderRoute();
})();
