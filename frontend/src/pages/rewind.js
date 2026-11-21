import {rewind} from '../api/rewind.js';
import {esc} from '../utils/format.js';
import {table} from '../components/common/table.js';
import {intervalRow} from '../components/rewind/interval-row.js';
import {bindForm} from '../hooks/forms.js';

const STATUS_TEXT = {
  ok: '可采用（计数唯一）',
  no_solution: '无解，已拒绝',
  ambiguous: '计数歧义，已拒绝',
  timeout: '计算超时，已拒绝',
  invalid: '输入非法，已拒绝',
};
const SHIFT_STATUS = {
  draft: '未采用',
  adopted: '已写入原检验',
  superseded: '采用后已更正（旧采用保留）',
};

export async function render(id) {
  const writable = localStorage.getItem('research-role') !== 'viewer';
  if (!id) {
    const shifts = await rewind.listShifts();
    return `<div class="eyebrow">05 / DIAL REWIND</div>
      <h1>表盘回卷还原</h1>
      <p>每班按区间录入工艺、总件与漏封两只表的回卷模数、起止读数及区间产量上限，再录全班实点。
      区间可跨多圈；漏封数不得超过总件数。无解、计数歧义或超时 5 秒分别拒绝。</p>
      ${writable ? newShiftForm() : ''}
      <h2>班次列表</h2>
      ${table(['编号', '名称', '版本', '区间数', '实点(总/漏)', '状态', '已采用', '操作'],
        shifts.map((s) => [
          s.id, esc(s.name), 'v' + s.version, s.interval_count,
          `${s.actual_total} / ${s.actual_missed}`,
          SHIFT_STATUS[s.status] || s.status,
          s.adopted ? '是' : '否',
          {html: `<a href="#rewind/${s.id}">查看还原依据 →</a>`},
        ]))}`;
  }
  const shift = await rewind.getShift(id);
  return detailView(shift, writable);
}

function newShiftForm() {
  return `<details ${location.hash.includes('/new') ? 'open' : ''}>
    <summary>新增班次录入</summary>
    <form id="shift-form">
      <div class="inline-form">
        <label>班次名称<input name="name" required minlength="1" maxlength="120" placeholder="如 11-21 早班"></label>
        <label>全班实点·总件<input name="actual_total" type="number" step="1" min="0" required></label>
        <label>全班实点·漏封<input name="actual_missed" type="number" step="1" min="0" required></label>
      </div>
      <h3>区间（两只表的模数、起止读数、上限）</h3>
      <div id="interval-list">${intervalRow(1)}</div>
      <div class="toolbar">
        <button type="button" id="add-interval" class="secondary">+ 增加区间</button>
        <button type="submit">保存并计算还原</button>
      </div>
    </form></details>`;
}

function detailView(shift, writable) {
  const latest = shift.restorations[0];
  return `<div class="eyebrow">05 / DIAL REWIND · SHIFT ${shift.id}</div>
    <h1>${esc(shift.name)} <small>v${shift.version} · ${SHIFT_STATUS[shift.status] || shift.status}</small></h1>
    <p>全班实点：总件 <b>${shift.actual_total}</b>，漏封 <b>${shift.actual_missed}</b>（漏封不超过总件）。</p>

    <h2>区间录入（${shift.intervals.length}）</h2>
    ${table(['#', '工艺', '总件模数', '漏封模数', '总件起→止', '漏封起→止', '产量上限'],
      shift.intervals.map((iv) => [
        iv.seq, iv.process, iv.mod_total, iv.mod_missed,
        `${iv.total_start} → ${iv.total_end}`,
        `${iv.missed_start} → ${iv.missed_end}`, iv.upper,
      ]))}

    ${writable ? amendForm(shift) : ''}
    <div class="toolbar">
      ${writable ? `<button id="run-restore" class="secondary">按当前版重新计算还原</button>` : ''}
      <a class="button secondary" href="#rewind">← 返回班次列表</a>
    </div>

    <h2>还原结论与依据</h2>
    ${shift.restorations.length ? restorationList(shift) : '<p class="method-note">尚无还原记录。</p>'}
    ${latest && latest.status === 'ok' ? adoptPanel(shift, latest, writable) : ''}
    ${shift.adoption ? adoptedPanel(shift) : ''}`;
}

function restorationList(shift) {
  return shift.restorations.map((r) => `
    <details class="worksheet restoration" ${r.id === shift.restorations[0].id ? 'open' : ''}>
      <summary>还原 #${r.id} · 基于 v${r.version} ·
        <span class="status-pill status-${r.status}">${STATUS_TEXT[r.status] || r.status}</span>
        <small>${r.elapsed_ms} ms</small></summary>
      ${r.status !== 'ok'
        ? `<div class="error">${esc(r.message || '该还原不可采用')}</div>`
        : okBody(r)}
    </details>`).join('');
}

function okBody(r) {
  const c = r.counts;
  return `
    <div class="result-intro">
      <strong>工艺 A：总件 ${c.A.total}，漏封 ${c.A.missed}；
      工艺 B：总件 ${c.B.total}，漏封 ${c.B.missed}</strong>
      <p>可行还原 ${r.feasible_solution_count ?? '大量'} 组；所有可行还原都给出同一组 A/B 计数，故计数唯一、可采用。</p>
    </div>
    <h3>逐区间还原依据</h3>
    ${table(['#', '工艺', '总件公式（止−起+模数×圈数）', '总件', '漏封公式', '漏封', '上限'],
      r.basis.map((b) => [
        b.index, b.process,
        esc(b.formula_total), b.total_count,
        esc(b.formula_missed), b.missed_count, b.upper,
      ]))}
    <details><summary>查看其它可行区间解（工艺计数仍相同）</summary>
      <pre>${esc(JSON.stringify(r.samples, null, 2))}</pre></details>`;
}

function adoptPanel(shift, r, writable) {
  if (shift.adoption) return '';
  if (r.version !== shift.version) {
    return `<div class="error">该还原基于 v${r.version}，当前数据为 v${shift.version}，请按当前版重新还原后再采用。</div>`;
  }
  if (!writable) return '<p class="method-note">观察员仅可查看，采用需分析师或负责人。</p>';
  return `<div class="toolbar adopt-bar">
    <form id="adopt-form">
      <input type="hidden" name="restoration_id" value="${r.id}">
      <input type="hidden" name="expected_version" value="${shift.version}">
      <button type="submit">采用该还原并写入原检验（v${shift.version}）</button>
    </form></div>`;
}

function adoptedPanel(shift) {
  const a = shift.adoption;
  return `<div class="method-note">
    <h3>已采用写入原检验</h3>
    <p>采用记录 #${a.id}，版本 v${a.version}，操作人 ${esc(a.created_by)}，时间 ${esc(a.created_at)}</p>
    <p>写入行：${a.written_keys.map((k) => `<code>${esc(k)}</code>`).join(' ')}</p>
    <pre>${esc(JSON.stringify(a.counts, null, 2))}</pre></div>`;
}

function amendForm(shift) {
  return `<details><summary>补录 / 更正（改动即升版，采用后更正会保留旧采用）</summary>
    <form id="amend-form">
      <div class="inline-form">
        <label>班次名称<input name="name" maxlength="120" value="${esc(shift.name)}"></label>
        <label>全班实点·总件<input name="actual_total" type="number" step="1" min="0" value="${shift.actual_total}"></label>
        <label>全班实点·漏封<input name="actual_missed" type="number" step="1" min="0" value="${shift.actual_missed}"></label>
      </div>
      <p class="method-note">区间整体替换；留空提交则仅更新名称与实点，保留现有区间。</p>
      <div id="amend-interval-list"></div>
      <div class="toolbar">
        <button type="button" id="amend-add-interval" class="secondary">+ 增加区间</button>
        <button type="submit">保存更正并升版</button>
      </div>
    </form></details>`;
}

export function bind(refresh) {
  const id = Number(location.hash.split('/')[1]) || 0;
  bindIntervalEditor('interval-list', 'add-interval');
  bindIntervalEditor('amend-interval-list', 'amend-add-interval');

  bindForm('shift-form', async (data) => {
    const body = {
      name: data.name,
      actual_total: Number(data.actual_total),
      actual_missed: Number(data.actual_missed),
      intervals: collectIntervals('interval-list'),
    };
    const created = await rewind.createShift(body);
    await rewind.restore(created.id);
    location.hash = `#rewind/${created.id}`;
    refresh();
  });

  bindForm('amend-form', async (data) => {
    const patch = {
      name: data.name,
      actual_total: Number(data.actual_total),
      actual_missed: Number(data.actual_missed),
    };
    const intervals = collectIntervals('amend-interval-list');
    if (intervals.length) patch.intervals = intervals;
    await rewind.amendShift(id, patch);
    await rewind.restore(id);
    refresh();
  });

  const runBtn = document.getElementById('run-restore');
  if (runBtn) runBtn.onclick = async () => {
    await rewind.restore(id);
    refresh();
  };

  bindForm('adopt-form', async (data) => {
    await rewind.adopt(id, Number(data.restoration_id), Number(data.expected_version));
    refresh();
  });
}

function bindIntervalEditor(listId, addBtnId) {
  const list = document.getElementById(listId);
  const addBtn = document.getElementById(addBtnId);
  if (!list || !addBtn) return;
  let seq = list.children.length;
  addBtn.onclick = () => {
    seq += 1;
    list.insertAdjacentHTML('beforeend', intervalRow(seq));
    wireRemove(list);
  };
  wireRemove(list);
}

function wireRemove(list) {
  list.querySelectorAll('.remove-interval').forEach((btn) => {
    btn.onclick = () => {
      btn.closest('.interval-row')?.remove();
      renumber(list);
    };
  });
}

function renumber(list) {
  [...list.children].forEach((row, i) => {
    row.dataset.seq = String(i + 1);
    row.querySelector('legend').textContent = `区间 ${i + 1}`;
  });
}

function collectIntervals(listId) {
  const list = document.getElementById(listId);
  if (!list) return [];
  return [...list.querySelectorAll('.interval-row')].map((row) => {
    const get = (name) => row.querySelector(`[name="${name}"]`).value;
    return {
      process: get('process'),
      mod_total: Number(get('mod_total')),
      mod_missed: Number(get('mod_missed')),
      total_start: Number(get('total_start')),
      total_end: Number(get('total_end')),
      missed_start: Number(get('missed_start')),
      missed_end: Number(get('missed_end')),
      upper: Number(get('upper')),
    };
  });
}
