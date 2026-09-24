const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (value) => 'GH₵' + Number(value).toLocaleString('en-GH', {minimumFractionDigits:2,maximumFractionDigits:2});
let state = {members:[], page:'overview'};
let requestKey = '';
let editingMemberId = null;
let paymentsPage = 1;
const canWrite = document.body.dataset.role === 'secretary';
let previewSequence = 0;
let previewTimer;
const csrf = () => document.cookie.split('; ').find(x=>x.startsWith('csrftoken='))?.split('=')[1] || '';
async function api(url, data) {
  const response = await fetch(url, {credentials:'same-origin', ...(data ? {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify(data)} : {})});
  if(response.status === 401){ location.href='/login/'; throw new Error('Please sign in again.'); }
  let result;
  try { result = await response.json(); } catch { throw new Error('The request could not be completed. Please reload and try again.'); }
  if(!response.ok) throw new Error(result.error || 'Could not complete the request.');
  return result;
}
function showError(id,message) { $(id).textContent=message; $(id).hidden=!message; }
function toast(message){ $('#toast').textContent=message; $('#toast').hidden=false; setTimeout(()=>$('#toast').hidden=true,4000); }
const badge = (s) => `<span class="badge ${s.toLowerCase().replace(' ','-')}">${esc(s)}</span>`;
function memberCell(m){const initials=m.name.trim().split(/\s+/).slice(0,2).map(x=>x[0]).join('');return `<div class="member-cell"><span class="member-initial">${esc(initials)}</span><div><button class="member-link" data-member="${m.id}">${esc(m.name)}</button><small>${esc(m.code)}</small></div></div>`;}
function paymentRows(items){return items.length ? items.map(p=>`<tr><td>${esc(p.receipt)} ${p.voided?'<span class="badge unpaid">VOID</span>':''}</td><td>${esc(p.member)}</td><td>${esc(p.date)}</td><td>${esc(p.method)}</td><td><b>${money(p.amount)}</b></td><td><a href="/receipts/${p.id}/" target="_blank" rel="noopener" class="text-button">Receipt ↗</a> ${canWrite&&!p.voided?`<button class="text-button danger" data-void="${p.id}" data-receipt="${esc(p.receipt)}">Void</button>`:''}</td></tr>`).join('') : '<tr><td colspan="6" class="empty">No payments yet. Record your first contribution.</td></tr>';}
function renderMembers(){const query=$('#member-search').value.toLowerCase(); const filter=$('#status-filter').value;const rows=state.members.filter(m=>(!filter||m.status===filter)&&`${m.name} ${m.code} ${m.phone}`.toLowerCase().includes(query));$('#members-table').innerHTML=rows.length ? rows.map(m=>`<tr><td>${memberCell(m)}</td><td>${esc(m.phone||'—')}</td><td>${esc(m.member_status)}</td><td>${money(m.paid)}</td><td>${money(m.balance)}</td><td>${badge(m.status)}</td></tr>`).join('') : '<tr><td colspan="6" class="empty">No matching members. Try another search or add a member.</td></tr>';}
async function refresh(){
  try {
    const data=await api('/api/overview/?month='+encodeURIComponent($('#month').value));
    state.members=data.members;
    $('#metric-collections').textContent=money(data.metrics.collections);
    $('#metric-outstanding').textContent=money(data.metrics.outstanding);
    $('#metric-paid').textContent=data.metrics.paid+' / '+data.metrics.active;
    $('#metric-active').textContent='Billable members · selected month';
    $('#metric-assigned').textContent=money(data.metrics.assigned);
    const active=state.members.filter(m=>m.status!=='Not due');
    const count=s=>active.filter(m=>m.status===s).length;
    $('#status-summary').innerHTML=`<div class="status-strip"><div class="status-bar"><span style="width:${active.length?count('Paid')/active.length*100:0}%;background:#6f9f6b"></span><span style="width:${active.length?count('Partial')/active.length*100:0}%;background:#e7c369"></span></div><div class="status-legend"><span><b>${count('Paid')}</b> paid</span><span><b>${count('Partial')}</b> partial</span><span><b>${count('Unpaid')}</b> unpaid</span></div></div>`;
    $('#overview-members').innerHTML=active.length?active.slice(0,5).map(m=>`<tr><td>${memberCell(m)}</td><td>${money(m.paid)}</td><td>${money(m.balance)}</td><td>${badge(m.status)}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No active members for this month. Add one in Members.</td></tr>';
    $('#recent-payments').innerHTML=paymentRows(data.recent);
    updateReportLinks();
    $('#arrears-summary').textContent='Cumulative arrears through selected month: '+money(data.metrics.arrears);
    renderMembers();
    if(state.page==='payments') await loadPayments();
    showError('#error-banner','');
  } catch(e){showError('#error-banner',e.message);}
}
async function loadPayments(){try {const result=await api('/api/payments/?page='+paymentsPage);$('#payments-table').innerHTML=paymentRows(result.payments);$('#payments-page').textContent='Page '+paymentsPage;$('#payments-prev').disabled=paymentsPage===1;$('#payments-next').disabled=!result.has_more;}catch(e){showError('#error-banner',e.message);}}
function go(page){state.page=page;$$('.page').forEach(el=>el.hidden=el.id!=='page-'+page);$$('.nav').forEach(el=>{el.classList.toggle('active',el.dataset.page===page);el.setAttribute('aria-current',el.dataset.page===page?'page':'false');});const titles={overview:['A clear view of your dues.','Track the month. Know who’s paid. Keep everything in order.'],members:['People behind the contributions.','Find a member, check their balance, or add someone new.'],payments:['Every payment, accounted for.','A single receipt for every contribution, however many months it covers.'],reports:['Close the month with confidence.','Member balances and payment statuses, ready for your records.']};$('#breadcrumb').textContent=page[0].toUpperCase()+page.slice(1);$('#page-title').textContent=titles[page][0];$('#page-subtitle').textContent=titles[page][1];if(page==='payments')loadPayments();}
$$('[data-page]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.page)));
$$('[data-go]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.go)));
$$('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
$('#month').addEventListener('change',refresh);
$('#member-search').addEventListener('input',renderMembers);
$('#status-filter').addEventListener('change',renderMembers);
function populatePaymentMembers(){const query=$('#payment-member-search').value.toLowerCase();const previous=$('#payment-member').value;const people=state.members.filter(m=>m.member_status==='Active'&&`${m.name} ${m.code}`.toLowerCase().includes(query));$('#payment-member').innerHTML='<option value="">Choose a member</option>'+people.map(m=>`<option value="${m.id}">${esc(m.name)} · ${esc(m.code)}</option>`).join('');if(people.some(m=>String(m.id)===previous))$('#payment-member').value=previous;else if(query && people.length===1)$('#payment-member').value=people[0].id;preview();}
function openPayment(){if(!state.members.some(m=>m.member_status==='Active')){go('members');toast('Add an active member first.');return;}$('#payment-form').reset();$('#payment-month').value=$('#month').value;requestKey=crypto.randomUUID();showError('#payment-error','');populatePaymentMembers();$('#payment-dialog').showModal();}
$('#record-payment').addEventListener('click',openPayment);$('#try-payment').addEventListener('click',openPayment);
$('#payment-member-search').addEventListener('input',populatePaymentMembers);
async function preview(){clearTimeout(previewTimer);const sequence=++previewSequence;$('#save-payment').disabled=true;const form=Object.fromEntries(new FormData($('#payment-form')));if(!form.member_id){$('#allocation-list').textContent='Choose a member to preview the split.';return;}$('#allocation-list').textContent='Calculating the months covered…';previewTimer=setTimeout(async()=>{try{const result=await api('/api/payments/preview/',form);if(sequence!==previewSequence)return;$('#allocation-list').innerHTML=result.allocations.map(a=>`<div class="allocation-row"><span>${esc(a.month)} <small>· ${esc(a.status)}</small></span><strong>${money(a.amount)}</strong></div>`).join('');$('#save-payment').disabled=false;showError('#payment-error','');}catch(e){if(sequence===previewSequence){$('#allocation-list').textContent=e.message;}}},180);}
['#payment-member','#payment-amount','#payment-month'].forEach(s=>$(s).addEventListener('input',preview));
$('#payment-form').addEventListener('submit',async e=>{e.preventDefault();const button=$('#save-payment');button.disabled=true;button.textContent='Saving…';try{const data=Object.fromEntries(new FormData(e.target));data.request_key=requestKey;const result=await api('/api/payments/',data);$('#payment-dialog').close();$('#success-text').textContent=`${money(result.amount)} saved as ${result.receipt}.`;$('#receipt-link').href=`/receipts/${result.id}/`;$('#success-dialog').showModal();await refresh();}catch(error){showError('#payment-error',error.message);}finally{button.disabled=false;button.textContent='Save payment & receipt';}});
$('#add-member').addEventListener('click',()=>{editingMemberId=null;$('#member-form').reset();$('#member-dialog h2').textContent='Add a member';showError('#member-error','');$('#member-dialog').showModal();});
$('#member-form').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('[type=submit]');button.disabled=true;try{const result=await api(editingMemberId?`/api/members/${editingMemberId}/`:'/api/members/',Object.fromEntries(new FormData(e.target)));$('#member-dialog').close();toast(editingMemberId?`${result.name} updated.`:`${result.name} added as ${result.code}.`);await refresh();}catch(error){showError('#member-error',error.message);}finally{button.disabled=false;}});
document.addEventListener('click',async e=>{const button=e.target.closest('[data-member]');if(!button)return;$('#profile-name').textContent='Member profile';$('#profile-content').textContent='Loading payment history…';$('#profile-dialog').showModal();try{const m=await api(`/api/members/${button.dataset.member}/`);$('#profile-name').textContent=m.name;$('#profile-content').innerHTML=`${canWrite?`<button class="button secondary" data-edit-member="${button.dataset.member}">Edit member</button>`:''}<p>${esc(m.code)} · ${esc(m.phone||'No phone added')}</p><p>${esc(m.email)}</p><div class="profile-total"><span>Total paid to date</span><strong>${money(m.total)}</strong><p>Cumulative arrears through this month: ${money(m.arrears)}</p></div><h2>Payment history</h2>${m.payments.length?m.payments.map(p=>`<div class="allocation-preview"><div class="preview-heading"><strong>${money(p.amount)} · ${esc(p.date)}</strong><a target="_blank" rel="noopener" href="/receipts/${p.id}/">${esc(p.receipt)} ↗</a></div><p>${esc(p.method)} ${p.voided?'<strong class="danger">VOID — excluded from balances</strong>':''}</p>${p.allocations.map(a=>`<div class="allocation-row"><span>${esc(a.month)}</span><span>${money(a.amount)}</span></div>`).join('')}</div>`).join(''):'<p>No payments recorded yet.</p>'}`;}catch(error){$('#profile-content').textContent=error.message;}});
document.addEventListener('DOMContentLoaded',refresh);
