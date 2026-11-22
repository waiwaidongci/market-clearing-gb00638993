import {feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {HOLD} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const ROW_COLUMNS = [
  {key:'ledger',label:'记账来源'}, {key:'transaction_key',label:'流水号'},
  {key:'posting_date',label:'入账日'}, {key:'kind',label:'记账类型'},
  {key:'original_key',label:'原核销流水号'}, {key:'zone',label:'校区'},
  {key:'merchant_group',label:'档口组'},
  {label:'金额（分）',render:row=>value(row.kind === '核销' ? row.amount_cents : -row.amount_cents)},
];

function releaseForm(item, spec, refresh) {
  const message = feedback();
  const submit = button('整组放行', {class:'primary', type:'submit', 'data-testid':'release-submit'});
  return el('form', {class:'fields', onSubmit:handler(submit, message, async event => {
    const data = new FormData(event.target);
    await workspace.release({dataset_id:item.dataset_id, group_id:item.group_id,
      destination:data.get('destination'), note:data.get('note')});
    message.textContent = '已放行，请重跑本结算日以计入清分。';
    await refresh();
  })},
  el('label', {class:'field', for:'release-destination'}, el('span', {}, '放行去向'),
    el('select', {name:'destination', id:'release-destination', required:true},
      (spec.release_destinations || ['结算入账']).map(option => el('option', {value:option}, option)))),
  el('label', {class:'field', for:'release-note'}, el('span', {}, '复核说明'),
    el('textarea', {id:'release-note', name:'note', maxlength:500, rows:3})),
  submit, message);
}

function group(item, spec, refresh) {
  const canRelease = item.status === 'pending' && session.canReview;
  return panel(`${item.group_id} · ${HOLD[item.status] || item.state_label}`,
    pill(HOLD[item.status] || item.state_label, item.status === 'held' ? 'held' : item.status),
    p(`牵涉流水：${(item.keys || []).join('、') || '—'}${item.anchors?.length ? `；原核销：${item.anchors.join('、')}` : ''}`),
    item.issues?.length ? el('ul', {class:'method-list'}, item.issues.map(issue => el('li', {}, issue.message))) : null,
    table(ROW_COLUMNS, item.rows || [], {caption:`${item.group_id} 扣留流水`, pageSize:8}),
    item.status === 'held'
      ? p('补正数据导入后，本组自动转为待放行；需整组重新校验通过才可放行。', 'subtle')
      : p(item.status === 'released'
        ? `放行去向：${item.destination || '—'} · ${item.author || ''} · ${date(item.created_at)}`
        : '补正数据已导入且整组校验通过，等待复核主管放行。', 'subtle'),
    item.note && p(item.note, 'subtle'),
    canRelease ? releaseForm(item, spec, refresh) : null);
}

export async function quarantinePage(route, refresh) {
  const {spec} = workspace;
  const items = await workspace.quarantine();
  const selected = items.find(item => item.group_id === route.key) || items[0];
  const list = table([
    {label:'扣留组', render:row => link(row.group_id, `#quarantine/${row.group_id}`)},
    {label:'状态', render:row => pill(HOLD[row.status] || row.state_label, row.status === 'held' ? 'held' : row.status)},
    {key:'keys', label:'牵涉流水', render:row => (row.keys || []).join('、')},
    {key:'anchors', label:'原核销', render:row => (row.anchors || []).join('、') || '—'},
    {key:'destination', label:'放行去向', render:row => row.destination || '—'},
    {label:'更新时间', render:row => date(row.created_at)},
  ], items, {caption:'扣留组', empty:'当前没有扣留流水。'});
  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'}, panel(spec.pages[6],
      p('导入时把有错的原核销与牵连的两账流水整组隔离，隔日退扣一并扣留；无关餐券照常清分。整组校验通过并放行后才计入结算。', 'subtle'),
      list),
      selected ? group(selected, spec, refresh) : empty('导入后若出现坏退扣，扣留组会显示在这里。', '#inputs', '前往导入')),
    el('aside', {class:'sidebar-panel'},
      panel('放行口径', el('ul', {class:'method-list'}, [
        el('li', {}, '同一退扣在两账挂了不同原单时，两笔原单都要扣留。'),
        el('li', {}, '补正后整组重新校验，双账齐全且逐笔一致才可放行。'),
        el('li', {}, '放行与清分并发时，一次清分只读到放行前或放行后的整组流水。'),
        el('li', {}, '同一补正组重复放行不重复入账。'),
      ]))));
}
