import { download, get, post } from '../api/client.js';
import { empty, table } from '../components/common/data.js';
import { intro } from '../components/common/frame.js';
import { formError } from '../components/common/forms.js';
import { Role, VersionStatus, WindowAction, reportStatusName } from '../constants/enums.js';
import { bindDownloads, bindForm, notify } from '../hooks/actions.js';
import { esc, formatMetric, timeLabel } from '../utils/html.js';

const fallbackZones = ['北栈桥', '贝壳湾', '南礁滩'];
const fallbackShifts = ['早班', '下午班'];

const choiceOf = (spec, key, fallback) =>
  spec.fields?.find(field => field.key === key)?.choices || fallback;

function clock(minute) {
  if (minute === 1440) return '24:00';
  return `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
}

function reportStatus(row) {
  let label = reportStatusName[row.status] || row.status;
  if (row.is_current) label = '当前版';
  else if (row.status === VersionStatus.SUCCEEDED) label = '历史版';
  return `<span class="status status-${esc(row.status)}">${esc(label)}</span>`;
}

function versionTable(rows) {
  return table([
    {label: '版本', html: row => `<a href="#shift/${row.id}">第 ${row.version} 版</a>`},
    {label: '状态', html: reportStatus},
    {label: '含潮窗更正', html: row => row.correction_ids?.length ? `${row.correction_ids.length} 份` : '无（原班报）'},
    {label: '算完时间', html: row => timeLabel(row.finished_at)},
    {label: '补算', html: row => row.status === VersionStatus.FAILED ? `<button data-retry="${row.id}">重试补算</button>` : '—'},
  ], rows, '这个班次还没有班报版本。');
}

function windowTable(windows) {
  if (!windows?.rows?.length) return empty('当前没有生效的可作业潮窗。');
  return table(windows.columns, windows.rows);
}

function correctionsTable(corrections) {
  if (!corrections.length) return empty('这个班次还没有潮窗更正，班报按原班报潮窗计算。');
  const rows = corrections.flatMap(correction => correction.segments.map(segment => ({
    code: correction.code,
    action: segment.action,
    zone: segment.zone,
    period: `${clock(segment.start_minute)}–${clock(segment.end_minute)}`,
    issued_at: correction.issued_at,
    submitted_by: correction.submitted_by,
  })));
  return table([
    {key: 'code', label: '更正编号'},
    {key: 'zone', label: '海滩分区'},
    {key: 'action', label: '区间性质'},
    {key: 'period', label: '本地时段'},
    {label: '签发时间', html: row => timeLabel(row.issued_at)},
    {key: 'submitted_by', label: '提交人'},
  ], rows);
}

function correctionForm(user, spec, workDate, shift) {
  if (user.role === Role.VIEWER) return '<p class="panel-body">只读账号可查看班报与更正来源。更正由调度员或值班班长提交。</p>';
  const zones = choiceOf(spec, 'zone', fallbackZones);
  const shifts = choiceOf(spec, 'shift', fallbackShifts);
  const segmentRows = [0, 1, 2].map(index => `<div class="segment-row" data-segment>
    <label class="field"><span>分区</span><select name="zone"><option value="">请选择</option>${zones.map(zone => `<option value="${esc(zone)}">${esc(zone)}</option>`).join('')}</select></label>
    <label class="field"><span>性质</span><select name="action"><option value="${WindowAction.WORKABLE}">可作业</option><option value="${WindowAction.CLOSED}">禁作业</option></select></label>
    <label class="field"><span>开始（分钟）</span><input type="number" name="start_minute" min="0" max="1440" step="1" placeholder="360"></label>
    <label class="field"><span>结束（分钟）</span><input type="number" name="end_minute" min="1" max="1440" step="1" placeholder="390"></label>
  </div>`).join('');
  return `<form id="correction-form" class="panel-body">
    <div class="form-grid">
      <label class="field"><span>作业日期</span><input type="date" name="work_date" required value="${esc(workDate)}"></label>
      <label class="field"><span>保洁班次</span><select name="shift" required>${shifts.map(item => `<option value="${esc(item)}" ${item === shift ? 'selected' : ''}>${esc(item)}</option>`).join('')}</select></label>
      <label class="field"><span>更正编号</span><input name="code" required maxlength="96" placeholder="CW-20261117-01"></label>
      <label class="field"><span>分区签核门槛（%）</span><input type="number" name="target_coverage_pct" min="0" max="100" step="any" value="90"></label>
    </div>
    <p class="aside-note">一份更正可同时划定多个分区。同一分钟按最新签发的更正计算；未被更正覆盖的时段继续沿用原班报潮窗，未被覆盖的更正时段照样入账。</p>
    <div class="segment-rows">${segmentRows}</div>
    <button type="button" id="add-segment">再加一段</button>
    ${formError}
    <button class="primary">提交更正并补算班报</button>
  </form>`;
}

export async function shiftPage(spec, user, route) {
  const detail = route.id && route.id !== 'new';
  if (detail) {
    const report = await get(`/shift-reports/${route.id}`);
    const head = intro(
      `${report.work_date} ${report.shift} · 第 ${report.version} 版`,
      `${report.is_current ? '班报页面当前版' : '历史版本（不再作为当前版）'} · 含 ${report.correction_ids.length} 份潮窗更正`,
      reportStatus(report),
    );
    if (report.status === VersionStatus.QUEUED) {
      return `${head}<div class="empty" data-pending="${report.id}">新版班报正在补算，算齐后才会切换当前版。此页会自动刷新。</div>`;
    }
    if (report.status === VersionStatus.FAILED) {
      return `${head}<div class="empty" role="alert">${esc(report.error)} <button data-retry="${report.id}">重试补算</button> <a href="#shift">返回班报</a></div>`;
    }
    const data = report.result;
    const metrics = data.metrics.map(metric => `<div><dt>${esc(metric.label)}</dt><dd>${esc(formatMetric(metric.value))}${metric.unit ? `<small> ${esc(metric.unit)}</small>` : ''}</dd></div>`).join('');
    return `${head}
      <section class="conclusion"><h2>${esc(data.headline)}</h2><p>${esc(data.summary)}</p><dl class="metrics">${metrics}</dl></section>
      <section class="panel"><div class="panel-heading"><h2>覆盖与异常明细</h2>${report.status === VersionStatus.SUCCEEDED ? `<button data-download="/shift-reports/${report.id}/export">下载 CSV</button>` : ''}</div>${table(data.columns, data.rows, '当前参数下无明细，计算结论保留。')}</section>
      <section class="panel"><div class="panel-heading"><h2>生效潮窗来源</h2></div>${windowTable(data.windows)}</section>
      <section class="panel"><div class="panel-heading"><h2>本版采用的潮窗更正</h2></div>${correctionsTable(report.corrections)}</section>
      <p class="aside-note"><a href="#shift?date=${esc(report.work_date)}&shift=${esc(report.shift)}">查看本班全部版本与来源 →</a></p>`;
  }

  const workDate = route.date || '2026-11-17';
  const shift = route.shift || '早班';
  const shifts = choiceOf(spec, 'shift', fallbackShifts);
  const [history, index] = await Promise.all([
    get(`/shift-reports/history?work_date=${encodeURIComponent(workDate)}&shift=${encodeURIComponent(shift)}`),
    get('/shift-reports'),
  ]);
  const current = history.current;
  const summary = current
    ? `<p class="aside-note">当前版（第 ${current.version} 版）：${esc(current.status === VersionStatus.SUCCEEDED ? '已算齐' : reportStatusName[current.status])}，${timeLabel(current.finished_at)} 完成。</p>`
    : '<p class="aside-note">这个班次还没有当前版班报。提交潮窗更正或录入台账后会在后台补算。</p>';
  return `${intro('班报与潮窗更正', '更正按最新签发计算覆盖；算齐后一次性切换当前版，旧版和来源仍可查。')}
    <div class="split"><section class="panel"><div class="panel-heading"><h2>${esc(workDate)} ${esc(shift)} 当前班报</h2></div>
      <div class="panel-body"><form id="shift-picker" class="form-grid">
        <label class="field"><span>作业日期</span><input type="date" name="work_date" value="${esc(workDate)}"></label>
        <label class="field"><span>保洁班次</span><select name="shift">${shifts.map(item => `<option value="${esc(item)}" ${item === shift ? 'selected' : ''}>${esc(item)}</option>`).join('')}</select></label>
        <button>查看</button>
      </form>${summary}
      ${current ? `<p><a class="text-link" href="#shift/${current.id}">打开当前版明细 →</a></p>` : ''}
      </div></section>
      <aside class="method"><h2>切版规则</h2><p>同一分钟按最新签发的潮窗更正计算。补算使用原班报的完工轨迹，覆盖率与异常一次算齐后才切换当前版；先到的更正即使晚算完，也不会让班报退回旧版。未被覆盖的更正时段照样入账。</p></aside></div>
    <section class="panel"><div class="panel-heading"><h2>版本记录</h2></div>${versionTable(history.versions)}</section>
    <section class="panel"><div class="panel-heading"><h2>提交潮窗更正</h2></div>${correctionForm(user, spec, workDate, shift)}</section>
    <section class="panel"><div class="panel-heading"><h2>更正来源</h2></div>${correctionsTable(history.corrections)}</section>
    <section class="panel"><div class="panel-heading"><h2>全部班次</h2></div>${table([
      {label: '作业日期', key: 'work_date'},
      {label: '班次', key: 'shift'},
      {label: '当前版', html: row => row.current ? `<a href="#shift/${row.current.id}">第 ${row.current.version} 版 · ${reportStatusName[row.current.status] || row.current.status}</a>` : '补算中'},
      {label: '版本数', key: 'versions'},
    ], index, '还没有班报。')}</section>`;
}

function collectSegments(form) {
  return [...form.querySelectorAll('[data-segment]')].flatMap(node => {
    const zone = node.querySelector('[name="zone"]').value;
    const start = node.querySelector('[name="start_minute"]').value;
    const end = node.querySelector('[name="end_minute"]').value;
    if (!zone && start === '' && end === '') return [];
    if (!zone || start === '' || end === '') throw new Error('每一段潮窗都要填分区、开始和结束分钟');
    return [{zone, action: node.querySelector('[name="action"]').value,
             start_minute: Number(start), end_minute: Number(end)}];
  });
}

export function wireShift(root, refresh) {
  bindDownloads(root, download);
  root.querySelectorAll('[data-retry]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await post(`/shift-reports/${button.dataset.retry}/retry`, {});
      notify('已重新排入补算');
      refresh();
    } catch (err) { notify(err.message, true); button.disabled = false; }
  }));
  const picker = root.querySelector('#shift-picker');
  if (picker) {
    picker.addEventListener('submit', event => {
      event.preventDefault();
      const date = picker.elements.work_date.value;
      const shift = picker.elements.shift.value;
      location.hash = `#shift?date=${date}&shift=${shift}`;
    });
  }
  const add = root.querySelector('#add-segment');
  if (add) {
    add.addEventListener('click', () => {
      const rows = root.querySelector('.segment-rows');
      const template = rows.firstElementChild.cloneNode(true);
      template.querySelectorAll('input').forEach(input => { input.value = ''; });
      rows.append(template);
    });
  }
  bindForm(root.querySelector('#correction-form'), async form => {
    const values = Object.fromEntries(new FormData(form));
    const segments = collectSegments(form);
    if (!segments.length) throw new Error('请至少填写一段潮窗');
    const report = await post('/corrections', {
      work_date: values.work_date,
      shift: values.shift,
      code: values.code,
      target_coverage_pct: Number(values.target_coverage_pct),
      segments,
    });
    notify(`更正已提交，第 ${report.report.version} 版补算中`);
    const target = `#shift?date=${values.work_date}&shift=${values.shift}`;
    if (location.hash === target) refresh();
    else location.hash = target;
  });
  const pending = root.querySelector('[data-pending]');
  if (pending) setTimeout(() => {
    if (pending.isConnected && location.hash === `#shift/${pending.dataset.pending}`) refresh();
  }, 800);
}
