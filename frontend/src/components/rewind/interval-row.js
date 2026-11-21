import {esc} from '../../utils/format.js';

// 单个区间录入行：工艺、两表模数、起止读数、上限
export function intervalRow(seq, value = {}) {
  const v = (k) => (value[k] ?? '');
  const num = (name, label, val) => `
    <label class="cell">${label}
      <input name="${name}" type="number" step="1" min="0" required value="${esc(val)}" data-seq="${seq}">
    </label>`;
  return `
  <fieldset class="interval-row" data-seq="${seq}">
    <legend>区间 ${seq}</legend>
    <label class="cell">工艺
      <select name="process" data-seq="${seq}" required>
        <option value="A" ${v('process') === 'A' ? 'selected' : ''}>A</option>
        <option value="B" ${v('process') === 'B' ? 'selected' : ''}>B</option>
      </select>
    </label>
    ${num('mod_total', '总件模数', v('mod_total'))}
    ${num('mod_missed', '漏封模数', v('mod_missed'))}
    ${num('total_start', '总件起', v('total_start'))}
    ${num('total_end', '总件止', v('total_end'))}
    ${num('missed_start', '漏封起', v('missed_start'))}
    ${num('missed_end', '漏封止', v('missed_end'))}
    ${num('upper', '产量上限', v('upper'))}
    <button type="button" class="text remove-interval" data-seq="${seq}">移除</button>
  </fieldset>`;
}
