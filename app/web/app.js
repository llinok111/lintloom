/* LintLoom 前端。零框架、零构建，所有状态都在内存里。
   唯一的对外通道是 /api/*，每个请求都带一次性 token。 */

(() => {
  'use strict';

  const CFG = window.__LINTLOOM__ || {};
  const API = '/api';
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    mode: 'file',
    root: null,
    rootPath: '',
    path: '',
    target: null,        // {root, path, type}
    roots: [],
    catalog: { dialects: [], rules: [], groups: [] },
    selectedRules: new Set(),
    groupFilter: new Set(),
    env: null,
    jobId: null,
    pollTimer: null,
    result: null,
    lastAction: null,
    codeCache: new Map(),
    expectedShas: {},    // file -> sha256，供 fix_apply 做并发保护
    diffFiles: [],
  };

  // ------------------------------------------------------------------ HTTP
  async function api(path, options = {}) {
    const headers = Object.assign(
      { 'X-LintLoom-Token': CFG.token },
      options.body ? { 'Content-Type': 'application/json' } : {},
      options.headers || {}
    );
    const res = await fetch(API + path, Object.assign({}, options, { headers }));
    let payload = null;
    try { payload = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!res.ok || (payload && payload.ok === false)) {
      const msg = (payload && payload.error) || `HTTP ${res.status}`;
      const err = new Error(msg);
      err.code = payload && payload.code;
      throw err;
    }
    return payload ? payload.data : null;
  }

  // ------------------------------------------------------------------ 工具
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  const fmtBytes = (n) => {
    if (!n && n !== 0) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(2) + ' MB';
  };
  const fmtMs = (s) => (s >= 1 ? s.toFixed(2) + ' s' : (s * 1000).toFixed(1) + ' ms');

  function toast(msg, kind) {
    setStatus(msg, kind);
  }

  function setStatus(text) {
    $('#statusText').textContent = text;
  }

  function setProgress(percent, label) {
    $('#bar').style.width = Math.max(0, Math.min(100, percent)) + '%';
    if (label) $('#statusText').textContent = label;
  }

  // ------------------------------------------------------------------ 环境
  async function loadEnv() {
    try {
      const env = await api('/env');
      state.env = env;
      const host = $('#envBadges');
      host.innerHTML = '';
      const up = env.upstream || {};
      addBadge(`sqlfluff ${up.version || '?'}`, env.catalog && env.catalog.ok);
      addBadge(`方言 ${env.catalog ? env.catalog.dialects.length : 0}`, env.catalog && env.catalog.ok);
      addBadge(`规则 ${env.catalog ? env.catalog.rules.length : 0}`, env.catalog && env.catalog.ok);
      addBadge(env.roots.some((r) => r.writable) ? '可写回' : '只读', true);
      if (!env.catalog.ok) {
        addBadge('环境异常', false);
        renderDiagnosis([{
          severity: 'error',
          code: 'ENV',
          message: env.catalog.error || '能力目录不可用',
          hint: env.catalog.hint || '请运行 安装.cmd',
          detail: '',
        }]);
        switchTab('diag');
      }
      $('#dialectMeta').textContent = env.catalog.source === 'python-api'
        ? '来自 sqlfluff Python API'
        : '来自 CLI 文本解析';
      return env;
    } catch (e) {
      addBadge('服务不可达', false);
      setStatus('无法连接后端：' + e.message);
      return null;
    }
  }

  function addBadge(text, good) {
    const el = document.createElement('span');
    el.className = 'badge ' + (good ? 'good' : 'bad');
    el.innerHTML = '<span class="dot"></span>' + esc(text);
    $('#envBadges').appendChild(el);
  }

  // ------------------------------------------------------------------ 目录树
  async function loadRoots() {
    const data = await api('/tree');
    state.roots = data.roots || [];
    const sel = $('#rootSel');
    sel.innerHTML = '';
    state.roots.forEach((r) => {
      const opt = document.createElement('option');
      opt.value = r.label;
      opt.textContent = `${r.label} — ${r.path}`;
      sel.appendChild(opt);
    });
    sel.onchange = () => {
      state.root = sel.value;
      state.target = null;
      $('#selection').textContent = '未选择目标';
      state.codeCache.clear();
      loadTree('');
    };
    state.root = state.roots.length ? state.roots[0].label : null;
    state.rootPath = '';
    state.path = '';
    if (state.root) await loadTree('');
    else {
      $('#tree').innerHTML = '<li><span class="muted">没有可访问的根目录</span></li>';
    }
  }

  async function loadTree(rel) {
    if (!state.root) return;
    const data = await api(`/tree?root=${encodeURIComponent(state.root)}&path=${encodeURIComponent(rel)}`);
    state.path = rel;
    renderCrumbs(rel);
    const ul = $('#tree');
    ul.innerHTML = '';
    if (rel) {
      const up = document.createElement('li');
      up.innerHTML = '<span class="ico">↑</span><span>上级目录</span>';
      up.onclick = () => loadTree(rel.split('/').slice(0, -1).join('/'));
      ul.appendChild(up);
    }
    const entries = data.entries || [];
    if (!entries.length) {
      ul.insertAdjacentHTML('beforeend', '<li><span class="muted">（空目录，或无受支持的 .sql 文件）</span></li>');
    }
    entries.forEach((entry) => {
      const li = document.createElement('li');
      const isDir = entry.type === 'dir';
      li.innerHTML = `<span class="ico">${isDir ? '▸' : '·'}</span><span>${esc(entry.name)}</span>`
        + (isDir ? '' : `<span class="size">${fmtBytes(entry.size)}</span>`);
      li.onclick = () => {
        if (isDir) loadTree(entry.path);
        else selectTarget(state.root, entry.path, 'file');
      };
      if (state.target && state.target.path === entry.path && !isDir) li.classList.add('on');
      ul.appendChild(li);
    });

    // 允许把整个根目录 / 当前目录作为目标
    const useDir = document.createElement('li');
    const atRoot = !rel;
    useDir.innerHTML = `<span class="ico">◎</span><span>${atRoot ? '把整个根目录作为目标' : '把当前目录整体作为目标'}</span>`;
    useDir.onclick = () => selectTarget(state.root, rel || '.', 'dir');
    ul.appendChild(useDir);
  }

  function renderCrumbs(rel) {
    const parts = rel ? rel.split('/') : [];
    const html = [`<a data-rel="">${esc(state.root || '/')}</a>`];
    parts.forEach((part, i) => {
      const sub = parts.slice(0, i + 1).join('/');
      html.push(`<a data-rel="${esc(sub)}">${esc(part)}</a>`);
    });
    const el = $('#crumbs');
    el.innerHTML = html.join(' / ');
    el.querySelectorAll('a').forEach((a) => {
      a.onclick = () => loadTree(a.dataset.rel || '');
    });
  }

  function selectTarget(root, path, type) {
    state.target = { root, path, type };
    $('#selection').innerHTML = `<b>${type === 'dir' ? '目录' : '文件'}</b> ${esc(root)}/${esc(path || '.')}`;
    $$('#tree li').forEach((li) => li.classList.remove('on'));
    switchTab('code');
    if (type === 'file') loadCode(root, path);
  }

  // ------------------------------------------------------------------ 目录清单
  async function loadCatalog(force) {
    const cat = force ? await api('/catalog/reload', { method: 'POST', body: '{}' }) : await api('/catalog');
    state.catalog = cat;
    fillDialects(cat.dialects);
    fillRules(cat);
    if (!cat.ok) {
      renderDiagnosis([{
        severity: 'error', code: 'ENV',
        message: cat.error || '能力目录不可用',
        hint: cat.hint || '运行 安装.cmd 后刷新',
        detail: '',
      }]);
      switchTab('diag');
    }
    return cat;
  }

  function fillDialects(dialects) {
    const sel = $('#dialect');
    sel.innerHTML = '';
    (dialects || []).forEach((d) => {
      const opt = document.createElement('option');
      opt.value = d.label;
      opt.textContent = `${d.label} — ${d.name}`;
      sel.appendChild(opt);
    });
    const preferred = ['ansi', 'postgres', 'mysql', 'bigquery', 'duckdb', 'tsql'];
    const hit = preferred.find((p) => (dialects || []).some((d) => d.label === p)) || (dialects[0] && dialects[0].label);
    if (hit) sel.value = hit;
    const update = () => {
      const cur = (dialects || []).find((d) => d.label === sel.value);
      $('#dialectHint').textContent = cur
        ? `${cur.name}${cur.inherits_from !== 'nothing' ? ' · 继承自 ' + cur.inherits_from : ' · 基础方言'}`
        : '';
    };
    sel.onchange = update;
    update();
  }

  function fillRules(cat) {
    const groups = cat.groups || [];
    const chips = $('#groupChips');
    chips.innerHTML = '';
    groups.forEach((g) => {
      const b = document.createElement('button');
      b.className = 'chip';
      b.textContent = g;
      b.onclick = () => {
        if (state.groupFilter.has(g)) state.groupFilter.delete(g);
        else state.groupFilter.add(g);
        b.classList.toggle('on');
        renderRuleList();
      };
      chips.appendChild(b);
    });
    renderRuleList();
    $('#ruleCount').textContent = `${cat.rules.length} 条`;
  }

  function renderRuleList() {
    const list = $('#ruleList');
    const filter = ($('#ruleFilter').value || '').trim().toLowerCase();
    const groups = state.groupFilter;
    list.innerHTML = '';
    let shown = 0;
    (state.catalog.rules || []).forEach((rule) => {
      const hitGroup = groups.size === 0 || (rule.groups || []).some((g) => groups.has(g));
      if (!hitGroup) return;
      const hay = `${rule.code} ${rule.name} ${rule.description}`.toLowerCase();
      if (filter && !hay.includes(filter)) return;
      shown += 1;
      const row = document.createElement('label');
      row.className = 'rule';
      const checked = state.selectedRules.has(rule.code);
      row.innerHTML = `<input type="checkbox" ${checked ? 'checked' : ''}>`
        + `<span><span class="code">${esc(rule.code)}</span><span class="rname">${esc(rule.name)}</span>`
        + `<span class="rdesc">${esc(rule.description)}</span></span>`;
      row.querySelector('input').onchange = (ev) => {
        if (ev.target.checked) state.selectedRules.add(rule.code);
        else state.selectedRules.delete(rule.code);
        updateRuleCount();
      };
      list.appendChild(row);
    });
    if (!shown) list.innerHTML = '<div class="rule"><span class="muted">没有匹配的规则</span></div>';
    updateRuleCount();
  }

  function updateRuleCount() {
    const n = state.selectedRules.size;
    $('#ruleCount').textContent = n ? `已选 ${n} / ${state.catalog.rules.length}` : `${state.catalog.rules.length} 条`;
  }

  // ------------------------------------------------------------------ 作业
  function payloadBase() {
    const base = {
      dialect: $('#dialect').value,
      rules: Array.from(state.selectedRules),
      noqa: $('#noqa').checked,
      timeout: Number($('#timeout').value) || 60,
    };
    if (state.mode === 'buffer') {
      base.text = $('#buffer').value;
    } else {
      if (!state.target) throw new Error('请先在左侧选择文件或目录');
      base.root = state.target.root;
      base.path = state.target.path || '.';
    }
    return base;
  }

  async function startJob(action, extra) {
    if (state.jobId) { toast('已有作业在运行，请先取消或等待完成'); return; }
    let body;
    try {
      body = Object.assign({ action }, payloadBase(), extra || {});
      if (action === 'parse' && body.path === '.') throw new Error('语法树需要选中单个 .sql 文件');
      if (action === 'parse' && state.target && state.target.type === 'dir') throw new Error('语法树需要选中单个 .sql 文件');
    } catch (e) {
      setStatus(e.message);
      return;
    }
    setBusy(true);
    setProgress(2, '提交作业…');
    clearViewsFor(action);
    try {
      const data = await api('/jobs', { method: 'POST', body: JSON.stringify(body) });
      state.jobId = data.job_id;
      state.lastAction = action;
      pollJob();
    } catch (e) {
      setBusy(false);
      setProgress(0, '提交失败');
      renderDiagnosis([{
        severity: 'error', code: e.code || 'REQUEST',
        message: '请求被拒绝：' + e.message,
        hint: '这通常意味着参数没有通过后端校验（路径越界、方言不存在、规则码拼错等）。',
        detail: '',
      }]);
      switchTab('diag');
    }
  }

  function setBusy(busy) {
    ['#btnLint', '#btnPreview', '#btnApply', '#btnParse'].forEach((sel) => {
      const el = $(sel);
      if (el) el.disabled = busy;
    });
    $('#btnCancel').hidden = !busy;
  }

  async function pollJob() {
    if (!state.jobId) return;
    let data;
    try {
      data = await api(`/jobs/${state.jobId}`);
    } catch (e) {
      setStatus('轮询失败：' + e.message);
      return;
    }
    const p = data.progress || {};
    setProgress(p.percent || 0, p.label || data.status);
    renderLog(data.events || []);
    const statBits = [];
    if (p.total) statBits.push(`<span><b>${p.done}</b>/${p.total} 文件</span>`);
    if (data.duration) statBits.push(`<span>${fmtMs(data.duration)}</span>`);
    statBits.push(`<span>${esc(data.status)}</span>`);
    $('#statLine').innerHTML = statBits.join('');

    if (data.status === 'running' || data.status === 'pending') {
      state.pollTimer = setTimeout(pollJob, 320);
      return;
    }
    state.jobId = null;
    setBusy(false);
    clearTimeout(state.pollTimer);
    if (data.status === 'done') {
      setProgress(100, '完成');
      renderResult(data);
    } else if (data.status === 'cancelled') {
      setProgress(0, '已取消');
      setStatus('作业已取消，子进程已回收');
    } else {
      setProgress(0, data.status === 'timeout' ? '超时' : '失败');
      setStatus((data.status === 'timeout' ? '超时：' : '失败：') + (data.error || '未知错误'));
      renderDiagnosis((data.result && data.result.diagnosis) || [{
        severity: 'error', code: data.error_code || 'JOB',
        message: data.error || '作业失败', hint: '查看「运行日志」里的 stderr 原文。', detail: '',
      }]);
      switchTab('diag');
      if (data.result) renderResult(data);
    }
  }

  async function cancelJob() {
    if (!state.jobId) return;
    try {
      await api(`/jobs/${state.jobId}/cancel`, { method: 'POST', body: '{}' });
      setStatus('已发出取消请求，正在回收进程树…');
    } catch (e) {
      setStatus('取消失败：' + e.message);
    }
  }

  // ------------------------------------------------------------------ 渲染结果
  function clearViewsFor(action) {
    if (action === 'lint' || action === 'lint_text') {
      $('#issuesBody').hidden = true;
      $('#issuesEmpty').hidden = false;
      $('#issuesEmpty').textContent = '诊断进行中…';
    }
    $('#logBody').hidden = false;
    $('#logEmpty').hidden = true;
  }

  function renderResult(job) {
    const result = job.result || {};
    if (result.diagnosis) renderDiagnosis(result.diagnosis);

    if (result.action === 'lint') {
      renderIssues(result);
      renderTiming(result);
      switchTab('issues');
    } else if (result.action === 'lint_text') {
      renderBufferIssues(result);
      switchTab('issues');
    } else if (result.action === 'fix_preview' || result.action === 'fix_apply') {
      renderDiff(result);
      switchTab('diff');
    } else if (result.action === 'fix_text') {
      renderTextFix(result);
      switchTab('diff');
    } else if (result.action === 'parse') {
      renderSyntax(result);
      switchTab('syntax');
    }
    updateTabCounts(result);
  }

  function updateTabCounts(result) {
    let issues = 0;
    let diffs = 0;
    let diag = 0;
    if (result.action === 'lint') issues = result.summary.violations;
    if (result.action === 'lint_text') issues = result.summary.violations;
    if (result.action === 'fix_text') { issues = result.summary.after; diffs = result.diff ? 1 : 0; }
    if (result.action === 'fix_preview') {
      diffs = result.summary.files_changed;
      issues = (result.files || []).reduce((acc, f) => acc + (f.remaining || 0), 0);
    }
    if (result.action === 'fix_apply') issues = (result.applied || []).reduce((a, f) => a + (f.remaining || 0), 0);
    diag = (result.diagnosis || []).length;
    $('#tabIssues').textContent = issues;
    $('#tabDiff').textContent = diffs;
    $('#tabDiag').textContent = diag;
  }

  function renderIssues(result) {
    const body = $('#issuesBody');
    const empty = $('#issuesEmpty');
    const files = result.files || [];
    const total = result.summary.violations;
    $('#statLine').innerHTML = `<span><b>${total}</b> 项</span><span>${result.summary.clean_files}/${result.summary.files_scanned} 文件干净</span><span>可自动修 <b>${result.summary.fixable}</b></span><span>${fmtMs(result.summary.duration)}</span>`;

    if (!files.length) {
      empty.hidden = false; body.hidden = true;
      empty.textContent = '没有结果';
      return;
    }
    empty.hidden = true; body.hidden = false;

    // 记录 sha256，供「应用修复」做并发保护
    state.expectedShas = {};

    const html = [];
    files.forEach((file) => {
      const violations = file.violations || [];
      if (file.sha256) state.expectedShas[file.file] = file.sha256;
      html.push(`<div class="fileblock" data-file="${esc(file.file)}">`);
      html.push('<header>'
        + `<span>${esc(file.root)}/${esc(file.file)}</span>`
        + `<span class="pill">${violations.length} 项</span>`
        + `<span class="pill">${fmtMs(file.duration)}</span>`
        + `<span class="cnt">${file.size} B · 退出码 ${file.exit_code}</span>`
        + '</header>');
      if (!violations.length) {
        html.push('<div class="vrow"><span class="pos">—</span><span class="code" style="color:var(--olive)">OK</span><span class="desc">无违规</span><span class="rname"></span><span class="fix y">—</span></div>');
      }
      violations.forEach((v) => {
        const fixable = (v.fixes || []).length > 0;
        html.push(`<div class="vrow ${v.warning ? 'warn' : ''}" data-file="${esc(file.file)}" data-line="${v.start_line_no}" data-pos="${v.start_line_pos}">`
          + `<span class="pos">${v.start_line_no}:${v.start_line_pos}</span>`
          + `<span class="code">${esc(v.code)}</span>`
          + `<span class="desc">${esc(v.description)}</span>`
          + `<span class="rname">${esc(v.name || '')}</span>`
          + `<span class="fix ${fixable ? 'y' : 'n'}">${fixable ? '可修 ' + v.fixes.length : '需人工'}</span>`
          + '</div>');
      });
      html.push('</div>');
    });
    body.innerHTML = html.join('');
    body.querySelectorAll('.vrow[data-line]').forEach((row) => {
      row.onclick = () => {
        const file = row.dataset.file;
        loadCode(state.target && state.target.root ? state.target.root : null, file, Number(row.dataset.line));
        switchTab('code');
      };
    });
    body.querySelectorAll('.fileblock > header').forEach((h) => {
      h.style.cursor = 'pointer';
      h.onclick = () => {
        const f = h.parentElement.dataset.file;
        loadCode(state.target ? state.target.root : null, f);
        switchTab('code');
      };
    });
    state.diffFiles = files.map((f) => ({ file: f.file, root: f.root }));
  }

  function renderBufferIssues(result) {
    const files = [{
      file: '编辑器缓冲区',
      root: 'stdin',
      violations: result.violations,
      size: '-',
      duration: result.summary.duration,
      exit_code: 0,
      root_: '',
    }];
    renderIssues({
      files,
      summary: {
        violations: result.summary.violations,
        fixable: result.summary.fixable,
        files_scanned: 1,
        clean_files: result.summary.violations ? 0 : 1,
        duration: result.summary.duration,
      },
      diagnosis: result.diagnosis,
    });
    // 缓冲区模式下，点击行跳回编辑器定位
    $$('#issuesBody .vrow[data-line]').forEach((row) => {
      row.onclick = () => {
        const ta = $('#buffer');
        const lines = ta.value.split('\n');
        const idx = Math.max(0, Number(row.dataset.line) - 1);
        let offset = 0;
        for (let i = 0; i < idx && i < lines.length; i += 1) offset += lines[i].length + 1;
        offset += Math.max(0, Number(row.dataset.pos) - 1);
        ta.focus();
        ta.setSelectionRange(offset, offset + 1);
      };
    });
  }

  function renderDiff(result) {
    const body = $('#diffBody');
    const empty = $('#diffEmpty');
    const files = result.files || [];
    const changed = files.filter((f) => f.changed);

    if (result.action === 'fix_apply') {
      const applied = result.applied || [];
      const skipped = result.skipped || [];
      empty.hidden = true; body.hidden = false;
      const html = [];
      html.push('<div class="difffile"><header><span>写回结果</span>'
        + `<span class="delta">写入 ${applied.length} · 跳过 ${skipped.length}</span></header>`);
      html.push('<div style="padding:10px 12px;font-size:12px">');
      if (result.backup_dir) html.push(`<p>备份目录：<code>${esc(result.backup_dir)}</code></p>`);
      applied.forEach((f) => {
        html.push(`<p>✔ <code>${esc(f.file)}</code> · 剩余 <b>${f.remaining}</b> 项<br>`
          + `<span class="muted">sha256 ${esc(f.sha_before.slice(0, 12))}… → ${esc(f.sha_after.slice(0, 12))}…</span></p>`);
      });
      skipped.forEach((f) => {
        html.push(`<p style="color:var(--gold)">— <code>${esc(f.file)}</code>：${esc(f.reason)}</p>`);
      });
      html.push('</div></div>');
      body.innerHTML = html.join('');
      $('#statLine').innerHTML = `<span>写入 <b>${applied.length}</b></span><span>跳过 ${skipped.length}</span><span>${fmtMs(result.summary.duration)}</span>`;
      refreshTreeSoon();
      return;
    }

    if (!changed.length) {
      empty.hidden = false; body.hidden = true;
      empty.textContent = files.length
        ? `检查了 ${files.length} 个文件，没有可自动修复的内容。`
        : '没有结果';
      $('#statLine').innerHTML = `<span>0 文件改动</span><span>${fmtMs(result.summary.duration)}</span>`;
      return;
    }
    empty.hidden = true; body.hidden = false;
    state.expectedShas = {};
    changed.forEach((f) => {
      if (f.before_sha256) state.expectedShas[f.file] = f.before_sha256;
    });
    const html = [];
    changed.forEach((f) => {
      const plus = (f.diff.match(/^\+(?!\+\+)/gm) || []).length;
      const minus = (f.diff.match(/^-(?!--)/gm) || []).length;
      html.push('<div class="difffile">');
      html.push(`<header><span>${esc(f.file)}</span>`
        + `<span class="delta"><span class="add">+${plus}</span> / <span class="del">−${minus}</span>`
        + ` · 复核剩余 ${f.remaining} 项${f.remaining_codes.length ? '（' + esc(f.remaining_codes.join(',')) + '）' : ''}</span></header>`);
      html.push('<pre class="diff">');
      f.diff.split('\n').forEach((line) => {
        let cls = 'ctx';
        if (line.startsWith('+++') || line.startsWith('---')) cls = 'meta';
        else if (line.startsWith('@@')) cls = 'hunk';
        else if (line.startsWith('+')) cls = 'add';
        else if (line.startsWith('-')) cls = 'del';
        html.push(`<div class="${cls}">${esc(line) || '&nbsp;'}</div>`);
      });
      html.push('</pre></div>');
    });
    body.innerHTML = html.join('');
    $('#statLine').innerHTML = `<span>改动 <b>${result.summary.files_changed}</b> 文件</span>`
      + `<span class="add">+${result.summary.lines_added}</span>`
      + `<span class="del">−${result.summary.lines_removed}</span>`
      + `<span>${fmtMs(result.summary.duration)}</span>`;
    state.lastPreview = result;
  }

  function renderTextFix(result) {
    const body = $('#diffBody');
    const empty = $('#diffEmpty');
    empty.hidden = true; body.hidden = false;
    const html = [];
    html.push('<div class="difffile"><header><span>编辑器缓冲区</span>'
      + `<span class="delta">${result.summary.before} 项 → ${result.summary.after} 项</span></header>`);
    if (result.diff) {
      html.push('<pre class="diff">');
      result.diff.split('\n').forEach((line) => {
        let cls = 'ctx';
        if (line.startsWith('+++') || line.startsWith('---')) cls = 'meta';
        else if (line.startsWith('@@')) cls = 'hunk';
        else if (line.startsWith('+')) cls = 'add';
        else if (line.startsWith('-')) cls = 'del';
        html.push(`<div class="${cls}">${esc(line) || '&nbsp;'}</div>`);
      });
      html.push('</pre>');
    } else {
      html.push('<div style="padding:12px" class="muted">没有可自动修复的内容。</div>');
    }
    html.push('<div style="padding:10px 12px"><button id="applyBufferFix">把修复结果写回编辑器</button></div>');
    html.push('</div>');
    body.innerHTML = html.join('');
    const btn = $('#applyBufferFix');
    if (btn) btn.onclick = () => {
      $('#buffer').value = result.fixed_text;
      updateBufferStat();
      setStatus('已把修复结果写回编辑器（未落盘）');
      switchTab('code');
    };
    $('#statLine').innerHTML = `<span><b>${result.summary.before}</b> → <b>${result.summary.after}</b></span><span>${fmtMs(result.summary.duration)}</span>`;
  }

  // ------------------------------------------------------------------ 代码视图
  async function loadCode(root, path, highlightLine) {
    if (!root || !path) return;
    const key = root + '/' + path;
    let file = state.codeCache.get(key);
    try {
      if (!file) {
        file = await api(`/file?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);
        state.codeCache.set(key, file);
      }
    } catch (e) {
      setStatus('读取文件失败：' + e.message);
      return;
    }
    state.expectedShas[path] = file.sha256;
    $('#codeEmpty').hidden = true;
    $('#codeBody').hidden = false;
    $('#codeHead').innerHTML = `<b>${esc(path)}</b><span>${file.lines} 行 · ${fmtBytes(file.size)}</span>`
      + `<span class="muted">sha256 ${esc(file.sha256.slice(0, 12))}…</span>`;
    const lines = file.content.split('\n');
    const pre = $('#codePre');
    pre.innerHTML = lines.map((text, i) => {
      const n = i + 1;
      const cls = highlightLine === n ? 'ln hit' : 'ln';
      return `<div class="${cls}"><span class="no">${n}</span><span class="tx">${esc(text) || ' '}</span></div>`;
    }).join('');
    if (highlightLine) {
      const hit = pre.querySelector('.ln.hit');
      if (hit) hit.scrollIntoView({ block: 'center' });
    }
    state.codeCacheTarget = { root, path };
  }

  // ------------------------------------------------------------------ 语法树
  function renderSyntax(result) {
    const body = $('#syntaxBody');
    const empty = $('#syntaxEmpty');
    empty.hidden = true; body.hidden = false;
    const rootSeg = result.segments || {};
    const keys = Object.keys(rootSeg);
    $('#syntaxMeta').textContent = `${result.file} · 退出码 ${result.exit_code} · ${fmtMs(result.duration)}`;
    const container = $('#syntaxTree');
    container.innerHTML = '';
    const build = (obj, depth) => {
      const frag = document.createDocumentFragment();
      Object.entries(obj).forEach(([key, value]) => {
        if (Array.isArray(value)) {
          const det = document.createElement('details');
          if (depth < 2) det.open = true;
          const sum = document.createElement('summary');
          sum.innerHTML = `<span>${esc(key)}</span><span class="count">${value.length}</span>`;
          det.appendChild(sum);
          value.forEach((child, i) => {
            if (child && typeof child === 'object') {
              const sub = document.createElement('div');
              sub.innerHTML = `<span class="count">[${i}]</span>`;
              sub.appendChild(build(child, depth + 1));
              det.appendChild(sub);
            }
          });
          frag.appendChild(det);
        } else if (value && typeof value === 'object') {
          const det = document.createElement('details');
          det.open = depth < 3;
          const sum = document.createElement('summary');
          sum.textContent = key;
          det.appendChild(sum);
          det.appendChild(build(value, depth + 1));
          frag.appendChild(det);
        } else {
          const div = document.createElement('div');
          div.className = 'leaf';
          div.innerHTML = `<span class="k">${esc(key)}</span>: <span class="v">${esc(JSON.stringify(value))}</span>`;
          frag.appendChild(div);
        }
      });
      return frag;
    };
    if (keys.length) container.appendChild(build(rootSeg, 0));
    else container.innerHTML = '<div class="muted">解析结果为空</div>';

    const filter = $('#syntaxFilter');
    filter.oninput = () => {
      const q = filter.value.trim().toLowerCase();
      container.querySelectorAll('details, .leaf').forEach((el) => {
        if (!q) { el.style.display = ''; return; }
        el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none';
      });
    };
  }

  // ------------------------------------------------------------------ 计时
  function renderTiming(result) {
    const files = (result.files || []).filter((f) => f.timings && Object.keys(f.timings).length);
    const body = $('#timingBody');
    const empty = $('#timingEmpty');
    if (!files.length) {
      empty.hidden = false; body.hidden = true;
      empty.textContent = '本次结果没有计时数据（上游未上报）。';
      return;
    }
    empty.hidden = true; body.hidden = false;
    const phases = ['templating', 'lexing', 'parsing', 'linting'];
    const html = [];
    files.forEach((f) => {
      const t = f.timings;
      const total = phases.reduce((acc, p) => acc + (t[p] || 0), 0) || 1;
      html.push('<div class="timing-file">');
      html.push(`<h4>${esc(f.file)} <span class="muted" style="font-weight:400">总 ${fmtMs(total)}</span></h4>`);
      phases.forEach((p, i) => {
        const v = t[p] || 0;
        const pct = (v / total) * 100;
        html.push(`<div class="phase p${i}"><span class="lbl">${p}</span>`
          + `<span class="track"><span class="fill" style="width:${pct.toFixed(1)}%"></span></span>`
          + `<span class="val">${fmtMs(v)}</span></div>`);
      });
      // 规则耗时 Top 8
      const rules = Object.entries(t)
        .filter(([k]) => /^[A-Z]{2}[0-9]{2}$/.test(k))
        .sort((a, b) => b[1] - a[1])
        .slice(0, 8);
      if (rules.length) {
        const max = rules[0][1] || 1;
        html.push('<div style="margin-top:8px"><div class="muted" style="font-size:11px;margin-bottom:4px">最耗时规则</div>');
        rules.forEach(([code, v]) => {
          html.push(`<div class="phase p2"><span class="lbl">${esc(code)}</span>`
            + `<span class="track"><span class="fill" style="width:${(v / max * 100).toFixed(1)}%"></span></span>`
            + `<span class="val">${fmtMs(v)}</span></div>`);
        });
        html.push('</div>');
      }
      const st = f.statistics || {};
      if (Object.keys(st).length) {
        html.push(`<div class="muted" style="font-size:11px;margin-top:8px">`
          + `源 ${st.source_chars} 字符 · 渲染后 ${st.templated_chars} · ${st.segments} segments / ${st.raw_segments} raw</div>`);
      }
      html.push('</div>');
    });
    body.innerHTML = html.join('');
  }

  // ------------------------------------------------------------------ 日志
  function renderLog(events) {
    const list = $('#logList');
    $('#logBody').hidden = false;
    $('#logEmpty').hidden = true;
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    list.innerHTML = events.map((ev) => {
      const extra = ev.data && ev.data.argv ? `<div class="muted" style="font-size:10.5px">argv: ${esc(JSON.stringify(ev.data.argv))}</div>` : '';
      return `<li><span class="t">${(ev.t || 0).toFixed(2)}s</span>`
        + `<span class="lv ${esc(ev.level)}">${esc(ev.level)}</span>`
        + `<span class="msg">${esc(ev.message)}${extra}</span></li>`;
    }).join('');
    if (atBottom) list.scrollTop = list.scrollHeight;
  }

  // ------------------------------------------------------------------ 诊断
  function renderDiagnosis(items) {
    const body = $('#diagBody');
    const empty = $('#diagEmpty');
    if (!items || !items.length) {
      empty.hidden = false; body.hidden = true;
      empty.textContent = '没有检测到异常。';
      $('#tabDiag').textContent = '0';
      return;
    }
    empty.hidden = true; body.hidden = false;
    body.innerHTML = items.map((d) => `
      <div class="diag-card ${esc(d.severity || 'error')}">
        <h4><span class="tag">${esc(d.code || '')}</span>${esc(d.message || '')}</h4>
        ${d.hint ? `<p class="hint">处置建议：${esc(d.hint)}</p>` : ''}
        ${d.detail ? `<pre>${esc(d.detail)}</pre>` : ''}
      </div>`).join('');
    $('#tabDiag').textContent = String(items.length);
  }

  // ------------------------------------------------------------------ 标签页
  function switchTab(name) {
    $$('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
    $$('.view').forEach((v) => v.classList.toggle('on', v.id === 'view-' + name));
  }

  // ------------------------------------------------------------------ 应用修复
  async function applyFix() {
    if (!state.lastPreview || !state.lastPreview.files) {
      setStatus('请先运行「修复预览」，确认差异后再写回');
      switchTab('diff');
      return;
    }
    const changed = state.lastPreview.files.filter((f) => f.changed);
    if (!changed.length) { setStatus('没有需要写回的文件'); return; }
    const expected = {};
    changed.forEach((f) => {
      const sha = state.expectedShas[f.file];
      if (sha) expected[f.file] = sha;
    });
    const list = changed.slice(0, 12).map((f) => `<li><code>${esc(f.file)}</code></li>`).join('');
    const more = changed.length > 12 ? `<li class="muted">… 另有 ${changed.length - 12} 个文件</li>` : '';
    openModal(
      '确认写回磁盘',
      `<p>将把 <b>${changed.length}</b> 个文件的修复结果写回原路径：</p><ul>${list}${more}</ul>`
      + '<p class="muted">原件会先备份到 <code>.lintloom-backups/</code>。写回前会校验 sha256 —— 若文件在预览后被改动过，该文件会被跳过而不是被覆盖。</p>',
      () => startJob('fix_apply', { confirm: true, expected })
    );
  }

  function openModal(title, html, onOk) {
    $('#modalTitle').textContent = title;
    $('#modalBody').innerHTML = html;
    $('#modal').hidden = false;
    $('#modalCancel').onclick = () => { $('#modal').hidden = true; };
    $('#modalOk').onclick = () => { $('#modal').hidden = true; onOk(); };
  }

  // ------------------------------------------------------------------ 其它
  function updateBufferStat() {
    const v = $('#buffer').value;
    $('#bufferStat').textContent = `${v.length} 字符 · ${v.split('\n').length} 行`;
  }

  function refreshTreeSoon() {
    state.codeCache.clear();
    setTimeout(() => { if (state.root) loadTree(state.path); }, 400);
  }

  // ------------------------------------------------------------------ 绑定
  function bind() {
    $('#modeSeg').onclick = (ev) => {
      const btn = ev.target.closest('button[data-mode]');
      if (!btn) return;
      state.mode = btn.dataset.mode;
      $$('#modeSeg button').forEach((b) => b.classList.toggle('on', b === btn));
      $('#filePane').hidden = state.mode !== 'file';
      $('#bufferPane').hidden = state.mode !== 'buffer';
      $('#selection').textContent = state.mode === 'file' ? (state.target ? `${state.target.root}/${state.target.path}` : '未选择目标') : '使用编辑器缓冲区（stdin）';
      $('#btnParse').disabled = state.mode !== 'file';
      $('#btnApply').disabled = state.mode === 'buffer';
    };

    $('#buffer').oninput = updateBufferStat;
    $('#bufferSample').onclick = () => {
      $('#buffer').value = [
        'select a.id,a.name , sum(b.amount) as total',
        'from orders a join order_items b on a.id=b.order_id',
        "where a.created_at > '2024-01-01' and b.status<>'cancelled'",
        'group by a.id,a.name',
        'having sum(b.amount)>100',
        'order by 3 desc',
      ].join('\n');
      updateBufferStat();
      setStatus('样例已载入，点「诊断」走 stdin 模式');
    };

    $('#btnLint').onclick = () => startJob(state.mode === 'buffer' ? 'lint_text' : 'lint');
    $('#btnPreview').onclick = () => startJob(state.mode === 'buffer' ? 'fix_text' : 'fix_preview');
    $('#btnApply').onclick = applyFix;
    $('#btnParse').onclick = () => startJob('parse');
    $('#btnCancel').onclick = cancelJob;

    $('#ruleFilter').oninput = renderRuleList;
    $('#rulesAll').onclick = () => {
      state.catalog.rules.forEach((r) => state.selectedRules.add(r.code));
      renderRuleList();
    };
    $('#rulesNone').onclick = () => {
      state.selectedRules.clear();
      renderRuleList();
    };
    $('#rulesCore').onclick = () => {
      state.selectedRules.clear();
      state.catalog.rules
        .filter((r) => (r.groups || []).includes('core'))
        .forEach((r) => state.selectedRules.add(r.code));
      renderRuleList();
    };

    $('#tabs').onclick = (ev) => {
      const btn = ev.target.closest('button[data-tab]');
      if (btn) switchTab(btn.dataset.tab);
    };

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') $('#modal').hidden = true;
      if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
        ev.preventDefault();
        $('#btnLint').click();
      }
    });
  }

  // ------------------------------------------------------------------ 启动
  async function boot() {
    bind();
    updateBufferStat();
    const env = await loadEnv();
    if (!env) return;
    try {
      await loadCatalog(false);
      await loadRoots();
      const sample = (state.catalog.rules || []).filter((r) => (r.groups || []).includes('core')).length;
      setStatus(`就绪 · ${state.catalog.dialects.length} 方言 / ${state.catalog.rules.length} 规则（core ${sample}）· Ctrl+Enter 快速诊断`);
    } catch (e) {
      setStatus('初始化失败：' + e.message);
    }
  }

  boot();
})();
