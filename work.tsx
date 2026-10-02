import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowDownToLine, ArrowLeft, ArrowRight, ArrowUpRight, Check, CircleAlert, CreditCard, FileText, LayoutDashboard, LogOut, Plus, Search, Settings2, ShieldCheck, Users, X } from 'lucide-react';
import { paletteWarnings, themeStyle } from './theme-bridge';
import { api, csrf, formData, money, signOut, type Branding, type Allocation, type AuditChange, type AuditDetail, type AuditEvent, type AuditPage, type Member, type MemberProfile, type Overview, type Payment, type Session } from './api';

type Page='overview'|'members'|'payments'|'reports'|'manage'|'audit'|'profile';
const names:Record<Page,string>={overview:'Overview',members:'Members',payments:'Payments',reports:'Reports',manage:'Manage',audit:'Audit history',profile:'Profile'};
const errorText=(e:unknown)=>e instanceof Error?e.message:'Please try again.';
const initials=(s:string)=>s.split(/\s+/).slice(0,2).map(x=>x[0]).join('');
// The shell has no router, so each page is given its own path and the address bar
// is what tells the app what to render. Sharing one URL for every page meant the
// back button walked straight out of the app and a reload always lost your place.
// Paths are built from the page name so a page cannot drift from its URL.
const PAGE_PATHS:Record<Page,string>={
  overview:'/overview/',members:'/members/',payments:'/payments/',reports:'/reports/',
  manage:'/manage/',audit:'/audit/',profile:'/profile/',
};
const stripSlash=(p:string)=>p.replace(/\/+$/,'');
// An unknown or bare path falls back to the overview rather than rendering a
// blank page, so a stale bookmark still lands somewhere useful.
const pageFromPath=():Page=>{
  const path=stripSlash(location.pathname);
  return (Object.keys(PAGE_PATHS)as Page[]).find(page=>stripSlash(PAGE_PATHS[page])===path)??'overview';
};
// Recording a payment is a workspace-wide action, but it only belongs on the
// pages where a balance is what the reader is looking at. Gating on the role
// alone left the button trailing along on Reports, Manage, Audit and the profile.
const takesPayments:Page[]=['overview','members','payments'];

function Alert({children}:{children:ReactNode}){return <div className="alert" role="alert"><CircleAlert size={17}/><span>{children}</span></div>;}
function Empty({children}:{children:ReactNode}){return <div className="empty"><FileText size={25}/><p>{children}</p></div>;}
function Badge({status}:{status:string}){return <span className={'badge '+status.toLowerCase().replaceAll(' ','-')}>{status}</span>;}
function Modal({title,subtitle,children,onClose,busy=false,wide=false}:{title:string;subtitle?:string;children:ReactNode;onClose:()=>void;busy?:boolean;wide?:boolean}){
 const ref=useRef<HTMLDialogElement>(null);
 useEffect(()=>{ref.current?.showModal();return()=>ref.current?.close();},[]);
 return <dialog ref={ref} className={wide?'wide':''} onCancel={e=>{e.preventDefault();if(!busy)onClose();}}><div className="dialog-top"><div><h2>{title}</h2></div><button className="icon-button" aria-label="Close" disabled={busy} onClick={onClose}><X size={20}/></button></div>{subtitle&&<p className="dialog-subtitle">{subtitle}</p>}{children}</dialog>;
}

export default function App(){
  const [session,setSession]=useState<Session|null>(null),[page,setPage]=useState<Page>(pageFromPath),[month,setMonth]=useState(''),[data,setData]=useState<Overview|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true),[revision,setRevision]=useState(0);
  const [paymentOpen,setPaymentOpen]=useState(false),[editMember,setEditMember]=useState<Member|null|undefined>(undefined),[memberProfile,setMemberProfile]=useState<number|null>(null),[voiding,setVoiding]=useState<Payment|null>(null),[saved,setSaved]=useState<{id:number;receipt:string;amount:string}|null>(null),[toast,setToast]=useState(''),[signingOut,setSigningOut]=useState(false);
  const write=session?.role==='secretary';
  const changed=(message='Saved successfully.')=>{setRevision(x=>x+1);setToast(message);};
  // Every navigation goes through go() so the address bar and the rendered page
  // cannot drift apart.
  const go=(target:Page)=>{setPage(target);setError('');const url=PAGE_PATHS[target];if(location.pathname!==url)history.pushState(null,'',url);};
  async function leave(){setSigningOut(true);setError('');try{await signOut();}catch(e){setError(errorText(e));setSigningOut(false);}}
  useEffect(()=>{const onPop=()=>setPage(pageFromPath());addEventListener('popstate',onPop);return()=>removeEventListener('popstate',onPop);},[]);
 useEffect(()=>{let alive=true;api<Session>('/api/session/').then(s=>{if(alive){setSession(s);setMonth(current=>current||s.today.slice(0,7));}}).catch(e=>{if(alive){setError(errorText(e));setLoading(false);}});return()=>{alive=false;};},[revision]);
 useEffect(()=>{if(!month)return;let alive=true;setLoading(true);api<Overview>('/api/overview/?month='+month).then(d=>{if(alive){setData(d);setError('');}}).catch(e=>{if(alive)setError(errorText(e));}).finally(()=>{if(alive)setLoading(false);});return()=>{alive=false;};},[month,revision]);
 useEffect(()=>{if(!toast)return;const t=setTimeout(()=>setToast(''),4500);return()=>clearTimeout(t);},[toast]);
 // The preview has to paint the live palette, so the unsaved values are set on a
// container rather than on the document. The document keeps the saved palette:
// painting it here would recolour the Settings page itself, which is not what
// somebody adjusting a colour asked for, and would leave the workspace mispainted
// if they navigated away without saving.
const [draft,setDraft]=useState<Palette|null>(null);
useEffect(()=>{if(!session)return;const b=session.branding;for(const key of ['primary','secondary','accent','primary_text','secondary_text','accent_text'] as const)document.documentElement.style.setProperty('--org-'+key.replace('_','-'),b[key]);return()=>{for(const key of ['primary','secondary','accent','primary-text','secondary-text','accent-text'])document.documentElement.style.removeProperty('--org-'+key);};},[session]);
 const navigation:{page:Page;icon:typeof Users}[]=[{page:'overview',icon:LayoutDashboard},{page:'members',icon:Users},{page:'payments',icon:CreditCard},{page:'reports',icon:FileText},...(write?[{page:'manage' as Page,icon:Settings2}]:[]),{page:'audit',icon:ShieldCheck}];
  const titles:Record<Page,[string,string]>={overview:['A clear view of your dues.','Track the month. Know who’s paid. Keep everything in order.'],members:['People behind the contributions.','Find a member, check their balance, or add someone new.'],payments:['Every payment, accounted for.','One receipt for every contribution, however many months it covers.'],reports:['Close the month with confidence.','Balances, collections and arrears, ready for your records.'],manage:['Your organisation, in order.','Manage accounts, import records and personalise receipts.'],audit:['A record you can follow.','Important changes, their reasons, and who made them.'],profile:['Your account.','Your details, your password, and how to sign out.']};
 if(!session)return <div className="opening"><div className="logo-mark">d.</div><h1>Duesdesk</h1>{error?<><Alert>{error}</Alert><button onClick={()=>location.reload()}>Try again</button></>:<p>Opening your workspace…</p>}</div>;
  return <div className="app-shell"><aside className="sidebar"><a className="brand" href={PAGE_PATHS.overview} onClick={e=>{e.preventDefault();go('overview');}}>{session.branding.logo_url?<img className="org-logo" src={session.branding.logo_url+'?v='+revision} alt={session.organisation+' logo'}/>:<span className="logo-mark">d.</span>}duesdesk<span>.</span></a><nav aria-label="Main navigation">{navigation.map(({page:p,icon:Icon})=><button key={p} className={page===p?'nav active':'nav'} aria-current={page===p?'page':undefined} onClick={()=>go(p)}><Icon size={19}/>{names[p]}</button>)}</nav><button type="button" className={'user-block'+(page==='profile'?' active':'')} aria-current={page==='profile'?'page':undefined} title="Your profile" onClick={()=>go('profile')}><span className="avatar">{initials(session.username)}</span><span className="user-block-text"><strong>{session.username}</strong><small>{session.role}</small></span></button></aside>
 <main className="main"><header className="topbar"><div>Workspace <span>/</span> <strong>{names[page]}</strong></div><div className="topbar-right"><span className="workspace-tag">{session.organisation}{session.demo?' · Test workspace':''}</span><a href="/account/password/" className="account-link">Change password</a><button className="signout" title="Sign out" aria-label="Sign out" disabled={signingOut} onClick={leave}><LogOut size={17}/></button></div></header><div className="content"><div className="page-heading"><div><h1>{titles[page][0]}</h1><p>{titles[page][1]}</p></div>{write&&takesPayments.includes(page)&&<button className="button primary" onClick={()=>setPaymentOpen(true)}><Plus size={17}/>Record payment</button>}</div>
 {['overview','members','reports'].includes(page)&&<div className="period-bar"><label htmlFor="report-month">Reporting month <input id="report-month" type="month" value={month} min="2000-01" onChange={e=>e.target.value&&setMonth(e.target.value)}/></label><span>All amounts in Ghana cedis (GH₵)</span></div>}
 {error&&<Alert>{error} <button className="text-button" onClick={()=>setRevision(x=>x+1)}>Retry</button></Alert>}
 {loading&&!data?<div className="loading" role="status">Loading your records…</div>:null}
 {data&&page==='overview'&&<><div className={'metrics '+(loading?'updating':'')}><Metric label="Money received this month" value={money(data.metrics.collections)} note="By the date payment was received"/><Metric label="Outstanding this month" value={money(data.metrics.outstanding)} note="All billable members · selected month"/><Metric label="Fully paid members" value={`${data.metrics.paid} / ${data.metrics.active}`} note="Members billed for the selected month"/><Metric label="Dues covered this month" value={money(data.metrics.assigned)} note="Includes payments received in advance"/></div><div className="overview-grid"><section className="panel"><div className="panel-heading"><div><h2>Monthly payment status</h2><p>A quick check on this month’s contributions.</p></div><button className="text-button" onClick={()=>go('members')}>View members <ArrowUpRight size={14}/></button></div><StatusBar members={data.members}/><MemberTable members={data.members.filter(m=>m.status!=='Not due').slice(0,5)} compact onProfile={setMemberProfile}/></section><section className="quick-guide"><h2>How payments are applied</h2><p>Choose a member and a starting month. We’ll allocate the payment to monthly dues of GH₵25.</p><div className="month-example">{[1,2,3,4].map(n=><span key={n}>0{n}<strong>25</strong></span>)}</div><p className="small">Already-paid months are skipped. Part payments cover the remaining balance.</p>{write&&<button className="button secondary" onClick={()=>setPaymentOpen(true)}>Record payment</button>}</section></div><section className="panel recent-panel"><div className="panel-heading"><div><h2>Recent payments</h2><p>One transaction, one receipt.</p></div><button className="text-button" onClick={()=>go('payments')}>All payments <ArrowUpRight size={14}/></button></div><PaymentTable items={data.recent} onVoid={write?setVoiding:undefined}/></section><div className="arrears-note"><span>Cumulative arrears through {formatMonth(month)}</span><strong>{money(data.metrics.arrears)}</strong><button className="text-button" onClick={()=>go('reports')}>See reports <ArrowRight size={14}/></button></div></>}
 {data&&page==='members'&&<Directory members={data.members} onProfile={setMemberProfile} onAdd={write?()=>setEditMember(null):undefined}/>}
 {page==='payments'&&<PaymentLedger revision={revision} onVoid={write?setVoiding:undefined}/>}
 {data&&page==='reports'&&<Reports month={month} data={data}/>}
  {page==='manage'&&write&&<Manage session={session} onChange={changed}/>}
  {page==='audit'&&<AuditLog/>}
  {page==='profile'&&<AccountPage session={session} busy={signingOut} onSignOut={leave}/>}
 <footer><strong>DUESDESK</strong><span>GH₵25 monthly dues · {session.demo?'Local test data':session.organisation}</span></footer></div></main>
 {paymentOpen&&<PaymentForm members={data?.members||[]} today={session.today} month={month} onClose={()=>setPaymentOpen(false)} onSaved={p=>{setPaymentOpen(false);setSaved(p);changed('Payment recorded.');}}/>}
 {editMember!==undefined&&<MemberForm member={editMember} today={session.today} onClose={()=>setEditMember(undefined)} onSaved={()=>{setEditMember(undefined);changed('Member saved.');}}/>}
 {memberProfile!==null&&<Profile memberId={memberProfile} month={month} canEdit={!!write} onClose={()=>setMemberProfile(null)} onEdit={m=>{setMemberProfile(null);setEditMember(m);}}/>}
 {voiding&&<VoidForm payment={voiding} onClose={()=>setVoiding(null)} onSaved={()=>{setVoiding(null);changed('Payment voided. Balances updated.');}}/>}
 {saved&&<Modal title="Payment recorded." onClose={()=>setSaved(null)}><div className="success-content"><span className="success-mark"><Check size={28}/></span><p>{money(saved.amount)} saved as <strong>{saved.receipt}</strong>.</p><a className="button primary" href={`/receipts/${saved.id}/`} target="_blank" rel="noopener noreferrer">View / print receipt <ArrowUpRight size={16}/></a><button className="button secondary" onClick={()=>setSaved(null)}>Done</button></div></Modal>}
 {toast&&<div className="toast" role="status"><Check size={16}/>{toast}</div>}
 </div>;
}

function formatMonth(month:string){return month?new Date(month+'-01T12:00:00').toLocaleDateString('en-GH',{month:'long',year:'numeric'}):'';}
// Everything shown here comes from /api/session/, which the backend already
// scopes to the signed-in user. There is no profile endpoint to read or write, so
// nothing on this page is editable and nothing is fetched on its behalf.
function AccountPage({session,busy,onSignOut}:{session:Session;busy:boolean;onSignOut:()=>void}){
  return <div className="account-page">
   <section className="panel"><div className="panel-heading"><div><h2>Profile</h2><p>The account you sign in with for {session.organisation}.</p></div></div>
    <dl className="account-details">
     <div><dt>Name</dt><dd>{session.username}</dd></div>
     <div><dt>Role</dt><dd>{session.role==='secretary'?'Secretary':'Auditor'}</dd></div>
     <div><dt>Organization</dt><dd>{session.organisation}</dd></div>
     <div><dt>Account status</dt><dd><span className="badge">Active</span></dd></div>
    </dl>
    <p className="account-note">Your role and organization are set by your organization&rsquo;s secretary. A secretary can also disable your access from Manage.</p>
   </section>
   <section className="panel"><div className="panel-heading"><div><h2>Security</h2><p>Your password protects every record in this workspace.</p></div></div>
    <div className="account-action"><div><strong>Password</strong><p>Set a new password at any time. You stay signed in on this device after changing it.</p></div><a className="button secondary" href="/account/password/">Change password</a></div>
   </section>
   <section className="panel"><div className="panel-heading"><div><h2>Account</h2><p>End this session on this device.</p></div></div>
    <div className="account-action"><div><strong>Sign out</strong><p>Your session ends immediately and you return to the sign in page.</p></div><button className="button secondary" disabled={busy} onClick={onSignOut}>{busy?'Signing out…':'Sign out'}</button></div>
   </section>
  </div>;
}
function Metric({label,value,note}:{label:string;value:string;note:string}){return <article className="metric"><span>{label}</span><strong>{value}</strong><small>{note}</small></article>;}
function StatusBar({members}:{members:Member[]}){const due=members.filter(m=>m.status!=='Not due'),count=(s:string)=>due.filter(m=>m.status===s).length;return <div className="status-strip"><div className="status-bar"><span style={{width:`${due.length?count('Paid')/due.length*100:0}%`}}/><span style={{width:`${due.length?count('Partial')/due.length*100:0}%`}}/></div><div className="status-legend"><span><b>{count('Paid')}</b> paid</span><span><b>{count('Partial')}</b> partial</span><span><b>{count('Unpaid')}</b> unpaid</span></div></div>;}
function MemberTable({members,compact=false,onProfile}:{members:Member[];compact?:boolean;onProfile:(id:number)=>void}){return !members.length?<Empty>No matching members. Try a different filter or add a member.</Empty>:<div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable data table"><table><thead><tr><th>Member</th>{!compact&&<><th>Phone</th><th>Membership</th></>}<th>Paid</th><th>Balance</th>{!compact&&<th>Cumulative arrears</th>}<th>Status</th></tr></thead><tbody>{members.map(m=><tr key={m.id}><td><div className="member-cell"><button className="member-name" onClick={()=>onProfile(m.id)}>{m.name}</button><small>{m.code}</small></div></td>{!compact&&<><td>{m.phone||'—'}</td><td>{m.member_status}</td></>}<td>{money(m.paid)}</td><td>{money(m.balance)}</td>{!compact&&<td>{money(m.arrears)}</td>}<td><Badge status={m.status}/></td></tr>)}</tbody></table></div>;}
function Directory({members,onProfile,onAdd}:{members:Member[];onProfile:(id:number)=>void;onAdd?:()=>void}){const [q,setQ]=useState(''),[status,setStatus]=useState('');const filtered=members.filter(m=>(!status||m.status===status)&&`${m.name} ${m.code} ${m.phone}`.toLowerCase().includes(q.toLowerCase()));return <section className="panel"><div className="panel-heading"><div><h2>Member directory <span className="count">{members.length}</span></h2><p>Choose a name to open the profile and payment history.</p></div>{onAdd&&<button className="button primary" onClick={onAdd}><Plus size={16}/>Add member</button>}</div><div className="filters"><div className="search-field"><Search size={17}/><input aria-label="Search members" type="search" value={q} onChange={e=>setQ(e.target.value)} placeholder="Search name, member ID or phone"/></div><select aria-label="Payment status" value={status} onChange={e=>setStatus(e.target.value)}><option value="">All payment statuses</option>{['Paid','Partial','Unpaid','Not due'].map(s=><option key={s}>{s}</option>)}</select></div><MemberTable members={filtered} onProfile={onProfile}/></section>;}
function PaymentTable({items,onVoid}:{items:Payment[];onVoid?:(p:Payment)=>void}){return !items.length?<Empty>No payments recorded yet.</Empty>:<div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable data table"><table><thead><tr><th>Receipt</th><th>Member</th><th>Date received</th><th>Method</th><th>Amount</th><th>Actions</th></tr></thead><tbody>{items.map(p=><tr key={p.id} className={p.voided?'voided':''}><td>{p.receipt} {p.voided&&<Badge status="Void"/>}</td><td>{p.member}</td><td>{p.date}</td><td>{p.method}</td><td><strong>{money(p.amount)}</strong></td><td><div className="row-actions"><a className="text-button" href={`/receipts/${p.id}/`} target="_blank" rel="noopener noreferrer">Receipt <ArrowUpRight size={13}/></a>{!p.voided&&onVoid&&<button className="text-button danger" onClick={()=>onVoid(p)}>Void</button>}</div></td></tr>)}</tbody></table></div>;}
function Pager({page,more,onChange}:{page:number;more:boolean;onChange:(n:number)=>void}){return <div className="pager"><button className="button secondary" disabled={page===1} onClick={()=>onChange(page-1)}><ArrowLeft size={14}/>Previous</button><span>Page {page}</span><button className="button secondary" disabled={!more} onClick={()=>onChange(page+1)}>Next<ArrowRight size={14}/></button></div>;}
function PaymentLedger({revision,onVoid}:{revision:number;onVoid?:(p:Payment)=>void}){const [page,setPage]=useState(1),[result,setResult]=useState<{payments:Payment[];has_more:boolean}|null>(null),[error,setError]=useState('');useEffect(()=>{let alive=true;setResult(null);api<{payments:Payment[];has_more:boolean}>('/api/payments/?page='+page).then(d=>{if(alive){setResult(d);setError('');}}).catch(e=>alive&&setError(errorText(e)));return()=>{alive=false;};},[page,revision]);return <section className="panel"><div className="panel-heading"><div><h2>Payment ledger</h2><p>All receipts, including advances and voided payments. 100 per page.</p></div></div>{error?<Alert>{error}</Alert>:result?<><PaymentTable items={result.payments} onVoid={onVoid}/><Pager page={page} more={result.has_more} onChange={setPage}/></>:<div className="loading">Loading payments…</div>}</section>;}

function MemberForm({member,today,onClose,onSaved}:{member:Member|null;today:string;onClose:()=>void;onSaved:()=>void}){const [busy,setBusy]=useState(false),[error,setError]=useState('');async function submit(e:FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);setError('');try{await api(member?`/api/members/${member.id}/`:'/api/members/',formData(e.currentTarget));onSaved();}catch(e){setError(errorText(e));}finally{setBusy(false);}}return <Modal title={member?'Edit member':'Add a member'} onClose={onClose} busy={busy}><form onSubmit={submit}><label>Full name<input name="name" defaultValue={member?.name} maxLength={100} required/></label><div className="form-row"><label>Phone<input name="phone" type="tel" defaultValue={member?.phone} maxLength={30}/></label><label>Email<input name="email" type="email" defaultValue={member?.email}/></label></div><div className="form-row"><label>Date joined<input name="joined" type="date" defaultValue={member?.joined||today} min="2000-01-01" max={today} required/></label><label>Membership status<select name="status" defaultValue={member?.member_status||'Active'}>{['Active','Inactive','Suspended'].map(x=><option key={x}>{x}</option>)}</select></label></div><label>Membership end month <span className="muted">(only if membership has ended)</span><input type="month" name="billing_end" defaultValue={member?.billing_end}/></label><div className="info-note">Monthly dues are GH₵25, but a payment can cover several months or part of a month. Leave the membership end month blank for ongoing members. Setting it blocks payments beyond that month.</div>{error&&<Alert>{error}</Alert>}<div className="dialog-actions"><button type="button" className="button secondary" onClick={onClose} disabled={busy}>Cancel</button><button className="button primary" disabled={busy}>{busy?'Saving…':'Save member'}</button></div></form></Modal>;}
function PaymentForm({members,today,month,onClose,onSaved}:{members:Member[];today:string;month:string;onClose:()=>void;onSaved:(p:{id:number;receipt:string;amount:string})=>void}){
 const [query,setQuery]=useState(''),[memberId,setMemberId]=useState(''),[amount,setAmount]=useState('100'),[start,setStart]=useState(month),[preview,setPreview]=useState<Allocation[]|null>(null),[previewError,setPreviewError]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[key]=useState(()=>crypto.randomUUID());
 const candidates=members.filter(m=>m.member_status==='Active'&&`${m.name} ${m.code}`.toLowerCase().includes(query.toLowerCase()));
 useEffect(()=>{let alive=true;setPreview(null);setPreviewError('');if(!memberId)return;const timer=setTimeout(()=>api<{allocations:Allocation[]}>('/api/payments/preview/',{member_id:memberId,amount,start_month:start}).then(d=>alive&&setPreview(d.allocations)).catch(e=>alive&&setPreviewError(errorText(e))),250);return()=>{alive=false;clearTimeout(timer);};},[memberId,amount,start]);
 async function submit(e:FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);setError('');try{const p=await api<{id:number;receipt:string;amount:string}>('/api/payments/',{...formData(e.currentTarget),member_id:memberId,amount,start_month:start,request_key:key});onSaved(p);}catch(e){setError(errorText(e));}finally{setBusy(false);}}
 return <Modal title="Record a payment" subtitle="One amount. A clear record of every month it covers." onClose={onClose} busy={busy}><form onSubmit={submit}><label>Find a member<input type="search" placeholder="Type a name or member ID" value={query} onChange={e=>setQuery(e.target.value)}/></label><label>Member<select value={memberId} onChange={e=>{setMemberId(e.target.value);const joined=members.find(m=>String(m.id)===e.target.value)?.joined.slice(0,7);if(joined&&start<joined)setStart(joined);}} required><option value="">Choose a member</option>{candidates.map(m=><option key={m.id} value={m.id}>{m.name} · {m.code}</option>)}{memberId&&!candidates.some(m=>String(m.id)===memberId)&&<option value={memberId}>{members.find(m=>String(m.id)===memberId)?.name}</option>}</select></label>{!members.some(m=>m.member_status==='Active')&&<Alert>Add an active member before recording a payment.</Alert>}<div className="form-row"><label>Amount (GH₵)<input type="number" min="0.01" max="3000" step="0.01" value={amount} onChange={e=>setAmount(e.target.value)} required/></label><label>Start covering from<input type="month" value={start} onChange={e=>setStart(e.target.value)} required/></label></div><div className="form-row"><label>Date received<input name="payment_date" type="date" defaultValue={today} min="2000-01-01" max={today} required/></label><label>Payment method<select name="method">{['Cash','Mobile Money','Bank Transfer','Check'].map(m=><option key={m}>{m}</option>)}</select></label></div><label>Transaction reference <span className="muted">(optional)</span><input name="reference" maxLength={80} placeholder="e.g. Mobile Money transaction ID"/></label><label>Notes <span className="muted">(optional)</span><input name="notes" maxLength={500}/></label><div className="allocation-preview"><div className="preview-heading"><strong>Months covered</strong><span>GH₵25 / month</span></div>{preview?preview.map(a=><div key={a.month} className="allocation-row"><span>{a.month} <small>· {a.status}</small></span><strong>{money(a.amount)}</strong></div>):<p>{!memberId?'Choose a member to preview the split.':previewError?'Check the payment details below.':'Calculating coverage…'}</p>}</div>{(error||previewError)&&<Alert>{error||previewError}</Alert>}<div className="dialog-actions"><button className="button secondary" type="button" onClick={onClose} disabled={busy}>Cancel</button><button className="button primary" disabled={busy||!memberId}>{busy?'Saving…':'Save payment & receipt'}</button></div></form></Modal>;
}
function Profile({memberId,month,canEdit,onClose,onEdit}:{memberId:number;month:string;canEdit:boolean;onClose:()=>void;onEdit:(m:Member)=>void}){const [data,setData]=useState<MemberProfile|null>(null),[error,setError]=useState('');useEffect(()=>{let alive=true;api<MemberProfile>(`/api/members/${memberId}/`).then(d=>alive&&setData(d)).catch(e=>alive&&setError(errorText(e)));return()=>{alive=false;};},[memberId]);return <Modal title={data?.name||'Member profile'} onClose={onClose} wide>{error?<Alert>{error}</Alert>:data?<><div className="profile-meta"><p>{data.code} · {data.phone||'No phone added'}<br/>{data.email}</p>{canEdit&&<button className="button secondary" onClick={()=>onEdit(data)}>Edit member</button>}</div><div className="profile-metrics"><div><span>Total paid to date</span><strong>{money(data.total)}</strong></div><div><span>Arrears through this month</span><strong>{money(data.arrears)}</strong></div></div>{canEdit&&<div className="download-actions"><a className="button primary" href={`/api/members/${memberId}/report/?month=${month}`}><ArrowDownToLine size={16}/>Download payment report (Excel)</a></div>}<h3>Payment history</h3>{data.payments.length?data.payments.map(p=><div key={p.id} className="allocation-preview"><div className="preview-heading"><strong>{money(p.amount)} · {p.date}</strong><a href={`/receipts/${p.id}/`} target="_blank" rel="noopener noreferrer">{p.receipt} ↗</a></div><p>{p.method} {p.voided&&<Badge status="Void"/>}</p>{p.voided&&<p className="danger">{p.void_reason}</p>}{p.allocations.map(a=><div className="allocation-row" key={a.month}><span>{a.month}</span><span>{money(a.amount)}</span></div>)}</div>):<Empty>No payments yet.</Empty>}</>:<p>Loading member…</p>}</Modal>;}
function VoidForm({payment,onClose,onSaved}:{payment:Payment;onClose:()=>void;onSaved:()=>void}){const [busy,setBusy]=useState(false),[error,setError]=useState('');async function submit(e:FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);try{await api(`/api/payments/${payment.id}/void/`,formData(e.currentTarget));onSaved();}catch(e){setError(errorText(e));}finally{setBusy(false);}}return <Modal title={`Void ${payment.receipt}?`} onClose={onClose} busy={busy}><p>This {money(payment.amount)} payment for {payment.member} will stop counting towards balances. The original receipt and reason remain in the history. Record a replacement afterwards if needed.</p><form onSubmit={submit}><label>Reason for correction<textarea name="reason" minLength={5} maxLength={500} required/></label>{error&&<Alert>{error}</Alert>}<div className="dialog-actions"><button type="button" className="button secondary" onClick={onClose} disabled={busy}>Cancel</button><button className="button danger-button" disabled={busy}>{busy?'Saving…':'Void payment'}</button></div></form></Modal>;}
function Reports({month,data}:{month:string;data:Overview}){const [kind,setKind]=useState('balances');const params=new URLSearchParams({month,kind});return <section className="panel report-card"><h2>Monthly report</h2><p>Download the {formatMonth(month)} report in Excel or CSV format.</p><label>Report type<select value={kind} onChange={e=>setKind(e.target.value)}><option value="balances">Member balances and cumulative arrears</option><option value="payments">Payments received and voided receipts</option></select></label><div className="download-actions"><a className="button primary" href={'/export/excel/?'+params}><ArrowDownToLine size={16}/>Download Excel</a><a className="button secondary" href={'/export/?'+params}><ArrowDownToLine size={16}/>Download CSV</a></div><div className="report-total"><span>Cumulative arrears through {formatMonth(month)}</span><strong>{money(data.metrics.arrears)}</strong></div><div className="report-help"><h3>How the totals work</h3><p><b>Money received</b> counts valid payments by the date the money was received.</p><p><b>Dues covered</b> counts allocations to the selected month, including money received earlier.</p><p><b>Cumulative arrears</b> adds unpaid dues from joining through the selected month, capped at the last billable month. Future allocations do not settle earlier debt.</p><p>Voided payments stay in the payment report for traceability, marked VOID. They are excluded from valid collection totals.</p></div></section>;}

type Account={id:number;username:string;email:string;role:string;active:boolean};
type Organisation=Branding&{contact:string;receipt_footer:string};
function Manage({session,onChange}:{session:Session;onChange:(s:string)=>void}){
 const [org,setOrg]=useState<Organisation|null>(null),[users,setUsers]=useState<Account[]|null>(null),[error,setError]=useState(''),[accountsError,setAccountsError]=useState(''),[disable,setDisable]=useState<Account|null>(null),[restore,setRestore]=useState<Account|null>(null),[adding,setAdding]=useState(false),[rev,setRev]=useState(0);
 // Settled separately so a branding failure cannot discard an account list that
 // loaded fine, and the other way round. A null list means "not loaded yet", which
 // is what separates the loading row from the empty one.
 useEffect(()=>{let alive=true;setError('');setAccountsError('');api<Organisation>('/api/settings/').then(o=>{if(alive)setOrg(o);}).catch(e=>{if(alive)setError(errorText(e));});api<{users:Account[]}>('/api/accounts/').then(u=>{if(alive)setUsers(u.users);}).catch(e=>{if(alive)setAccountsError(errorText(e));});return()=>{alive=false;};},[rev]);
 return <>{error&&<Alert>{error}</Alert>}<div className="manage-grid">{org?<BrandingForm key={org.id} org={org} onSaved={o=>{setOrg(o);onChange('Organization branding saved.');}}/>:<p>Loading organization…</p>}<ImportCard onSaved={onChange}/></div><SecretaryTeam/><section className="panel"><div className="panel-heading"><div><h2>Organization accounts</h2><p>Only accounts belonging to {session.organisation} appear here.</p></div><button className="button secondary" onClick={()=>setAdding(true)}>Add auditor</button></div>{accountsError?<Alert>{accountsError} <button className="text-button" onClick={()=>setRev(x=>x+1)}>Retry</button></Alert>:users===null?<p className="loading" role="status">Loading accounts…</p>:users.length===0?<Empty>No organization accounts yet.</Empty>:<div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable data table"><table><caption className="sr-only">Accounts belonging to {session.organisation}</caption><thead><tr><th>Username</th><th>Email</th><th>Role</th><th>Status</th><th>Actions</th></tr></thead><tbody>{users.map(u=><tr key={u.id}><td>{u.username}</td><td>{u.email||'—'}</td><td>{u.role}</td><td>{u.active?'Active':'Disabled'}</td><td>{u.active?(u.id!==session.user_id?<button className="text-button danger" aria-label={`Disable access for ${u.username}`} onClick={()=>setDisable(u)}>Disable access</button>:null):<button className="text-button" aria-label={`Restore access for ${u.username}`} onClick={()=>setRestore(u)}>Restore access</button>}</td></tr>)}</tbody></table></div>}</section>{disable&&<DisableAccount user={disable} onClose={()=>setDisable(null)} onSaved={()=>{setDisable(null);setRev(x=>x+1);onChange('Organization access disabled.');}}/>}{restore&&<RestoreAccount user={restore} onClose={()=>setRestore(null)} onSaved={()=>{setRestore(null);setRev(x=>x+1);onChange(`Access restored for ${restore.username}.`);}}/>}{adding&&<AddAuditor onClose={()=>setAdding(false)} onSaved={name=>{setAdding(false);setRev(x=>x+1);onChange(`Auditor ${name} added.`);}}/>}</>;
}
function BrandingForm({org,onSaved}:{org:Organisation;onSaved:(o:Organisation)=>void}){
 const [palette,setPalette]=useState({primary:org.primary,secondary:org.secondary,accent:org.accent}),[token,setToken]=useState(''),[remove,setRemove]=useState(false),[preview,setPreview]=useState(org.logo_url),[busy,setBusy]=useState(false),[error,setError]=useState('');
 async function upload(file?:File){setToken('');setError('');if(!file)return;setBusy(true);try{const data=new FormData();data.append('logo',file);const response=await fetch('/api/branding/preview/',{method:'POST',headers:{'X-CSRFToken':csrf()},body:data});const result=await response.json();if(!response.ok)throw Error(result.error||'Unable to upload logo.');setToken(result.logo_token);setPalette({primary:result.primary,secondary:result.secondary,accent:result.accent});setRemove(false);const reader=new FileReader();reader.onload=()=>setPreview(String(reader.result));reader.readAsDataURL(file);}catch(e){setError(errorText(e));}finally{setBusy(false);}}
 async function save(e:FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);setError('');try{const result=await api<Organisation>('/api/settings/',{...formData(e.currentTarget),...palette,logo_token:token,remove_logo:remove});setToken('');setRemove(false);setPreview(result.logo_url?result.logo_url+'?v='+Date.now():'');onSaved(result);}catch(e){setError(errorText(e));}finally{setBusy(false);}}
// The preview is the same markup the sign-up page draws, and it is driven by
 // the same six custom properties, so a palette chosen here and one chosen
 // during sign-up are the same thing seen in two places. It reads the live values
 // rather than the unsaved organisation, which is the point of a preview.
 return <section className="panel manage-card"><h2>Organization & branding</h2><form onSubmit={save}><label>Organization name<input name="name" defaultValue={org.name} maxLength={120} required/></label><label>Contact details<input name="contact" defaultValue={org.contact} maxLength={200}/></label><label>Receipt message<input name="receipt_footer" defaultValue={org.receipt_footer} maxLength={250}/></label>{preview&&!remove&&<img className="brand-preview" src={preview} alt="Organization logo preview"/>}<label>Upload logo<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={e=>upload(e.target.files?.[0])}/></label><p className="small">Up to 1 MB and 4 million pixels. Review and adjust the colors extracted from your logo.</p>{preview&&<button type="button" className="text-button danger" disabled={busy} onClick={()=>{setRemove(true);setToken('');setPreview('');}}>Remove logo</button>}
 <ThemeControls palette={palette} onChange={setPalette} busy={busy} logo={preview&&!remove?preview:''}/>
 {error&&<Alert>{error}</Alert>}<button className="button primary" disabled={busy}>{busy?'Processing…':'Save details & theme'}</button><p><a href={'/login/?org='+org.public_id}>Branded sign-in page</a></p></form></section>;
 }
type Palette={primary:string;secondary:string;accent:string};
const PALETTE_ROLES:{key:keyof Palette;label:string;role:string}[]=[
 {key:'primary',label:'Primary colour',role:'Sidebar, primary buttons, and the links inside your content.'},
 {key:'secondary',label:'Secondary colour',role:'The selected item in the sidebar, your avatar, and the workspace tag.'},
 {key:'accent',label:'Accent colour',role:'The logo mark, and the highlight a sidebar item takes on hover.'},
];

// One preview, used by both Settings and the sign-up page. It is drawn from the
// real class names and reads the same --org-* variables, so it cannot drift from
// the application. Nothing in it paints a colour and nothing in it is connected
// to a record: the names, codes and amounts are invented.
function ThemePreview({logo}:{logo?:string}){
 return <div className="theme-preview">
  <div className="theme-preview-bar"><span className="theme-preview-dots" aria-hidden="true"><i/><i/><i/></span><strong>Live preview</strong><span>Sample workspace — no real records</span></div>
  <div className="theme-preview-scroll"><div className="theme-preview-stage">
   <div className="app-shell">
    <aside className="sidebar">
     <span className="brand">{logo?<img className="org-logo" src={logo} alt=""/>:<span className="logo-mark">d.</span>}duesdesk<span>.</span></span>
     <nav aria-label="Preview navigation">
      <span className="nav active" aria-current="page"><LayoutDashboard size={14}/>Overview</span>
      <span className="nav"><Users size={14}/>Members</span>
      <span className="nav"><CreditCard size={14}/>Payments</span>
      <span className="nav"><FileText size={14}/>Reports</span>
     </nav>
     <span className="user-block"><span className="avatar">AO</span><span className="user-block-text"><strong>Sample secretary</strong><small>Secretary</small></span></span>
    </aside>
    <main className="main">
     <header className="topbar"><div>Workspace <span>/</span> <strong>Overview</strong></div><div className="topbar-right"><span className="workspace-tag">Sample workspace</span></div></header>
     <div className="content">
      <div className="page-heading"><div><h1>Overview</h1><p>Where the association stands this month.</p></div><span className="button primary"><Plus size={14}/>Record payment</span></div>
      <div className="metrics">
       <div className="metric"><span>Money received this month</span><strong>GH₵ 12,450</strong><small>By the date payment was received</small></div>
       <div className="metric"><span>Outstanding this month</span><strong>GH₵ 3,175</strong><small>All billable members</small></div>
       <div className="metric"><span>Fully paid members</span><strong>41 / 58</strong><small>Billed for this month</small></div>
       <div className="metric"><span>Dues covered this month</span><strong>GH₵ 15,625</strong><small>Including payments in advance</small></div>
      </div>
      <section className="panel">
       <div className="panel-heading"><div><h2>Member directory <span className="count">4</span></h2><p>Choose a name to open the profile and payment history.</p></div></div>
       <div className="table-wrap"><table>
        <caption className="sr-only">Sample member list showing each payment status</caption>
        <thead><tr><th>Member</th><th>Status</th><th>Arrears</th><th>Actions</th></tr></thead>
        <tbody>
         {[['Ama Mensah','MBR-0001','Paid','paid','—'],['Kwame Boateng','MBR-0002','Partial','partial','GH₵ 50'],['Akosua Owusu','MBR-0003','Unpaid','unpaid','GH₵ 175'],['Kofi Asante','MBR-0004','Not due','not-due','GH₵ 25']].map(([name,code,label,cls,arrears])=><tr key={code}><td><span className="member-cell"><span className="member-name">{name}</span><small>{code}</small></span></td><td><span className={'badge '+cls}>{label}</span></td><td>{arrears}</td><td><span className="row-actions"><span className="text-button">View</span></span></td></tr>)}
        </tbody>
       </table></div>
      </section>
      <div className="alert"><p><b>A sample notice.</b> Payment and audit colours stay the same in every workspace, so a failure always looks like a failure.</p></div>
     </div>
    </main>
   </div>
  </div></div>
  <p className="theme-preview-note">Every name, code and amount above is invented. Hover a sidebar item to see the accent colour.</p>
 </div>;
}

// Contrast is not decided here. branding_json already sends the readable
// foreground for each colour the server will store, and using those means the
// preview shows exactly the text colour the saved palette will produce.
function ThemeControls({palette,onChange,busy,logo}:{palette:Palette;onChange:(p:Palette)=>void;busy?:boolean;logo?:string}){
 // A theme.js that failed to load would otherwise throw inside render. Catching it
 // here means the rest of Settings stays usable and only the live text colour is
 // unavailable, which is a far better failure than a blank page.
 const [warn,setWarn]=useState<string>('');
 const style=useMemo(()=>{try{return themeStyle(palette);}catch{return {};}},[palette]);
 const warnings=useMemo(()=>{try{return paletteWarnings(palette);}catch(e){setWarn(e instanceof Error?e.message:'Preview unavailable.');return [];}},[palette]);
 return <fieldset className="theme-controls" disabled={busy}>
  <legend className="eyebrow">Workspace theme</legend>
  {PALETTE_ROLES.map(({key,label,role})=><div className="theme-row" key={key}>
   <input type="color" value={palette[key]} aria-label={label} onChange={e=>onChange({...palette,[key]:e.target.value})}/>
   <div><label>{label}</label><p className="theme-hex">{palette[key]}</p><p className="theme-role">{role}</p></div>
  </div>)}
  {warn&&<p className="small muted">{warn}</p>}
  <div className="theme-warnings" role="status" aria-live="polite">
   {warnings.map(w=><p className="theme-warning" key={w.message}><CircleAlert size={15}/><span>{w.message} <em>Contrast is {w.ratio}:1; {w.minimum}:1 is the minimum.</em></span></p>)}
  </div>
  <p className="small muted">Saved colours take effect across the workspace. Payment and audit colours never change.</p>
  {/* The variables land on the preview, not on the document: the workspace keeps
      the saved palette while this is being adjusted. */}
  <div style={style}><ThemePreview logo={logo}/></div>
 </fieldset>;
}

type Invitation={id:number;email:string;status:string;expires_at:string};
function SecretaryTeam(){
 const [items,setItems]=useState<Invitation[]>([]),[url,setUrl]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[leaving,setLeaving]=useState(false),[rev,setRev]=useState(0);
 useEffect(()=>{let alive=true;api<{invites:Invitation[]}>('/api/invites/').then(r=>{if(alive)setItems(r.invites);}).catch(e=>alive&&setError(errorText(e)));return()=>{alive=false;};},[rev]);
 async function invite(e:FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);setError('');setUrl('');try{const r=await api<{url:string}>('/api/invites/',formData(e.currentTarget));setUrl(r.url);setRev(x=>x+1);}catch(e){setError(errorText(e));}finally{setBusy(false);}}
 async function revoke(id:number){setBusy(true);setError('');try{await api('/api/invites/'+id+'/revoke/',{});setUrl('');setRev(x=>x+1);}catch(e){setError(errorText(e));}finally{setBusy(false);}}
 async function leave(){setBusy(true);setError('');try{await api('/api/organization/leave/',{});location.assign('/login/');}catch(e){setError(errorText(e));}finally{setBusy(false);}}
 return <section className="panel manage-card team-card"><h2>Invite Secretary</h2><p>Send a one-use invitation to another secretary. They create their own password and join your organization. Links expire after 7 days.</p><form onSubmit={invite}><label>Secretary email<input name="email" type="email" maxLength={254} required/></label><button className="button primary" disabled={busy}>Generate invitation</button></form>{url&&<div className="invite-link"><label>Copy and share this link privately<input aria-label="Invitation link" value={url} readOnly onFocus={e=>e.target.select()}/></label><p className="small">Only the specified email can accept. This link is shown once; no email has been sent.</p></div>}{error&&<Alert>{error}</Alert>}{items.length?<div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable data table"><table><thead><tr><th>Email</th><th>Status</th><th>Expires</th><th>Action</th></tr></thead><tbody>{items.map(i=><tr key={i.id}><td>{i.email}</td><td>{i.status}</td><td>{new Date(i.expires_at).toLocaleDateString()}</td><td>{i.status==='Pending'&&<button className="text-button danger" disabled={busy} onClick={()=>revoke(i.id)}>Revoke</button>}</td></tr>)}</tbody></table></div>:<p>No invitations yet.</p>}<hr/><button className="text-button danger" disabled={busy} onClick={()=>setLeaving(true)}>Leave Organization</button><p className="small">Another active secretary must remain. Your organization's records are retained.</p>{leaving&&<Modal title="Leave this organization?" onClose={()=>setLeaving(false)} busy={busy}><p>You will lose access and be signed out. Your members, payments and reports stay with the organization.</p>{error&&<Alert>{error}</Alert>}<div className="dialog-actions"><button className="button secondary" disabled={busy} onClick={()=>setLeaving(false)}>Stay</button><button className="button danger-button" disabled={busy} onClick={leave}>Leave Organization</button></div></Modal>}</section>;
}
function DisableAccount({user,onClose,onSaved}:{user:Account;onClose:()=>void;onSaved:()=>void}){const [busy,setBusy]=useState(false),[error,setError]=useState('');async function disable(){setBusy(true);try{await api(`/api/accounts/${user.id}/disable/`,{});onSaved();}catch(e){setError(errorText(e));}finally{setBusy(false);}}return <Modal title={`Disable ${user.username}?`} onClose={onClose} busy={busy}><p>This user will lose access immediately. Their recorded payments and audit history remain.</p>{error&&<Alert>{error}</Alert>}<div className="dialog-actions"><button className="button secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="button danger-button" disabled={busy} onClick={disable}>{busy?'Disabling…':'Disable account'}</button></div></Modal>;}
function RestoreAccount({user,onClose,onSaved}:{user:Account;onClose:()=>void;onSaved:()=>void}){const [busy,setBusy]=useState(false),[error,setError]=useState('');async function restore(){setBusy(true);try{await api(`/api/accounts/${user.id}/enable/`,{});onSaved();}catch(e){setError(errorText(e));}finally{setBusy(false);}}return <Modal title={`Restore access for ${user.username}?`} onClose={onClose} busy={busy}><p>This account can sign in again with its existing <strong>{user.role}</strong> access. The username, email and role are not changed.</p><p className="small">They will need their old password, or a reset link sent to the email above.</p>{error&&<Alert>{error}</Alert>}<div className="dialog-actions"><button className="button secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy?'Restoring…':'Restore access'}</button></div></Modal>;}
function AddAuditor({onClose,onSaved}:{onClose:()=>void;onSaved:(username:string)=>void}){
 const [busy,setBusy]=useState(false),[error,setError]=useState('');
 async function submit(e:FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);setError('');
  const data=formData(e.currentTarget);
  try{await api('/api/accounts/',{...data,role:'auditor'});onSaved(String(data.username||''));}
  catch(e){setError(errorText(e));}
  finally{setBusy(false);}}
 return <Modal title="Add an auditor" subtitle="Auditors can read members, payments and reports. They cannot record, edit or delete anything." onClose={onClose} busy={busy}>
  <form onSubmit={submit}>
   <label>Username<input name="username" maxLength={150} required/></label>
   <label>Email<input name="email" type="email" maxLength={254} required/></label>
   <label>Temporary password<input name="password" type="password" autoComplete="new-password" required/></label>
   <p className="small">Share this password privately. The auditor can replace it from the Change password link after signing in. The same password rules apply as for your own account.</p>
   {error&&<Alert>{error}</Alert>}
   <div className="dialog-actions"><button type="button" className="button secondary" disabled={busy} onClick={onClose}>Cancel</button><button className="button primary" disabled={busy}>{busy?'Adding…':'Add auditor'}</button></div>
  </form>
 </Modal>;
}
function ImportCard({onSaved}:{onSaved:(s:string)=>void}){const [kind,setKind]=useState('members'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[preview,setPreview]=useState<{token:string;count:number;preview:Record<string,string>[]}|null>(null);async function upload(e:FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);setError('');setPreview(null);try{const response=await fetch('/api/import/preview/',{method:'POST',credentials:'same-origin',headers:{'X-CSRFToken':csrf()},body:new FormData(e.currentTarget)});if(response.status===401){location.assign('/login/');return;}const result=await response.json();if(!response.ok)throw Error(result.error||'Could not read the file.');setPreview(result);}catch(e){setError(errorText(e));}finally{setBusy(false);}}async function commit(){if(!preview)return;setBusy(true);try{const r=await api<{count:number}>('/api/import/commit/',{token:preview.token});setPreview(null);onSaved(`${r.count} records imported.`);}catch(e){setError(errorText(e));}finally{setBusy(false);}}return <section className="panel manage-card"><h2>Import records</h2><p>Upload a CSV, review it, then confirm. Each file is imported completely or not at all.</p><form onSubmit={upload}><label>Import type<select name="kind" value={kind} onChange={e=>{setKind(e.target.value);setPreview(null);setError('');}}><option value="members">Members</option><option value="payments">Payments</option></select></label><p><a href={'/api/import/template/?kind='+kind}>Download CSV template <ArrowDownToLine size={13}/></a></p><label>CSV file<input type="file" name="file" accept=".csv,text/csv" onChange={()=>setPreview(null)} required/></label><p className="small">Up to 500 rows / 1 MB. Payments use the numeric member ID: 1 for MBR-0001. Allocations are recalculated when saved.</p><button className="button secondary" disabled={busy}>{busy?'Checking…':'Review import'}</button></form>{error&&<Alert>{error}</Alert>}{preview&&<div className="import-preview"><strong>{preview.count} rows ready · first {preview.preview.length} shown</strong><div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable data table"><table><thead><tr>{Object.keys(preview.preview[0]).map(k=><th key={k}>{k}</th>)}</tr></thead><tbody>{preview.preview.map((row,i)=><tr key={i}>{Object.entries(row).map(([k,v])=><td key={k}>{v||'—'}</td>)}</tr>)}</tbody></table></div><button className="button primary" disabled={busy} onClick={commit}>{busy?'Importing…':`Confirm ${preview.count}-row import`}</button></div>}</section>;}
// --- Audit history ----------------------------------------------------------
// The history is a record of what happened, so a row has to be identifiable at a
// glance and investigable once opened. Everything technical stays available, but
// it is the last section rather than the whole interface.
type AuditFilters={q:string;category:string;outcome:string;actor:string;since:string;until:string;security:boolean;request_id:string};
const NO_AUDIT_FILTERS:AuditFilters={q:'',category:'',outcome:'',actor:'',since:'',until:'',security:false,request_id:''};
const auditQuery=(f:AuditFilters,page:number)=>{
  const params=new URLSearchParams();
  if(f.q)params.set('q',f.q);
  if(f.category)params.set('category',f.category);
  if(f.outcome)params.set('outcome',f.outcome);
  if(f.actor)params.set('actor',f.actor);
  if(f.since)params.set('since',f.since);
  if(f.until)params.set('until',f.until);
  if(f.request_id)params.set('request_id',f.request_id);
  if(f.security)params.set('security','1');
  if(page>1)params.set('page',String(page));
  const query=params.toString();
  return query?'?'+query:'';
};
const TIME_UNITS:[string,number][]=[['minute',60],['hour',3600],['day',86400],['week',604800],['month',2629800],['year',31557600]];
// "12 minutes ago" is what a reader wants; the exact time stays on the element
// and in the expanded detail, so nothing is lost by leading with the relative one.
const relativeTime=(iso:string)=>{
  const seconds=Math.round((Date.now()-new Date(iso).getTime())/1000);
  if(!Number.isFinite(seconds)||seconds<60)return 'just now';
  const step=TIME_UNITS.find(([,size])=>seconds>=size)??TIME_UNITS[0];
  const count=Math.floor(seconds/step[1]);
  return `${count} ${step[0]}${count===1?'':'s'} ago`;
};
// Rendered inside a sentence, so a value that is not there must not show as
// "null" or "undefined" the way a bare String() would.
const show=(value:unknown)=>value===null||value===undefined||value===''?'—':typeof value==='string'?value:JSON.stringify(value);
const fieldName=(field:string)=>field.replaceAll('_',' ').replace(/^\w/,c=>c.toUpperCase());

function AuditChanges({changes}:{changes:AuditChange[]}){
  if(!changes.length)return null;
  return <table className="audit-changes"><caption className="sr-only">Values before and after this change</caption><thead><tr><th scope="col">Field</th><th scope="col">Before</th><th scope="col">After</th></tr></thead><tbody>{changes.map(c=><tr key={c.field}><th scope="row">{fieldName(c.field)}</th><td className="audit-was">{show(c.from)}</td><td className="audit-now">{show(c.to)}</td></tr>)}</tbody></table>;
}
function AuditSection({heading,children}:{heading:string;children:ReactNode}){
  // A section with nothing in it is dropped rather than shown empty: an empty
  // "Request" heading teaches a reader that some headings mean nothing.
  return <section className="audit-section"><h4>{heading}</h4>{children}</section>;
}
function AuditRow({event,onDrill}:{event:AuditEvent;onDrill:(requestId:string)=>void}){
  const [open,setOpen]=useState(false),[detail,setDetail]=useState<AuditDetail|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[retry,setRetry]=useState(0);
  // The list deliberately withholds the record, so it is fetched when the row is
  // opened and then kept, so a reader going back and forth does not re-request it.
  //
  // What has been loaded lives in a ref, not in `detail`. Were it state, setting
  // `busy` or `detail` would re-run this effect, and the cleanup would then
  // invalidate the very request that is in flight: the second run returns early
  // because `busy` is set, the response is discarded, and the row reads
  // "Loading details…" forever. A ref is not reactive, so it can guard the fetch
  // without being able to restart it. The only other dependency is `retry`,
  // which changes only when the reader asks for it.
  const loaded=useRef(0);
  useEffect(()=>{
    if(!open||loaded.current===event.id)return;
    let alive=true;
    setBusy(true);
    api<AuditDetail>('/api/audit/'+event.id+'/').then(d=>{if(alive){loaded.current=event.id;setDetail(d);setError('');}}).catch(e=>{if(alive)setError(errorText(e));}).finally(()=>{if(alive)setBusy(false);});
    return()=>{alive=false;};
  },[open,event.id,retry]);
  const exact=new Date(event.date).toLocaleString('en-GB',{dateStyle:'medium',timeStyle:'medium'});
  return <li className={'audit-row severity-'+event.severity}>
    <button type="button" className="audit-summary" aria-expanded={open} onClick={()=>setOpen(v=>!v)}>
      <span className={'audit-rail severity-'+event.severity} aria-hidden="true"/>
      <span className="audit-head"><strong>{event.label}</strong><span className="audit-meta">{event.resource&&<span className="audit-resource">{event.resource} · </span>}<span>{event.actor}{event.actor_role&&<span className="audit-role"> ({event.actor_role})</span>}</span></span></span>
      <span className={'audit-chip '+event.outcome}>{event.outcome_label}</span>
      <time className="audit-when" dateTime={event.date} title={exact}>{relativeTime(event.date)}</time>
    </button>
    {open&&<div className="audit-detail">
      <p className="audit-why">{detail?.reason_label||event.reason_label}</p>
      {error&&<Alert>{error} <button className="text-button" onClick={()=>{setError('');setRetry(n=>n+1);}}>Retry</button></Alert>}
      {busy&&!detail&&<p className="loading" role="status">Loading details…</p>}
      {detail&&<>
        <AuditSection heading="Who"><dl className="audit-facts"><div><dt>Account</dt><dd>{detail.actor}{detail.actor_role&&` · ${detail.actor_role}`}</dd></div></dl></AuditSection>
        <AuditSection heading="When"><dl className="audit-facts"><div><dt>Recorded</dt><dd>{exact}</dd></div><div><dt>Relative</dt><dd>{relativeTime(detail.date)}</dd></div></dl></AuditSection>
        {(detail.entity||detail.resource)&&<AuditSection heading="Affected record"><dl className="audit-facts">{detail.entity&&<div><dt>Type</dt><dd>{fieldName(detail.entity)}</dd></div>}{detail.resource&&<div><dt>Record</dt><dd>{detail.resource}</dd></div>}</dl></AuditSection>}
        {detail.changes.length>0&&<AuditSection heading="What changed"><AuditChanges changes={detail.changes}/></AuditSection>}
        {detail.request_id&&detail.related.length>0&&<AuditSection heading="Saved at the same time"><button className="text-button" onClick={()=>onDrill(detail.request_id)}>Show all {detail.related_count} events from this save</button><ul className="audit-related">{detail.related.map(r=><li key={r.id}><span className={'audit-rail severity-'+r.severity} aria-hidden="true"/><span>{r.label}</span><time dateTime={r.date}>{relativeTime(r.date)}</time></li>)}</ul></AuditSection>}
        <AuditSection heading="Where it came from"><dl className="audit-facts"><div><dt>{detail.ip_is_peer?'Connection address':'IP address'}</dt><dd>{detail.ip_address||'Not recorded'}{detail.ip_is_peer&&' · the reverse proxy, not the member'}</dd></div><div><dt>Browser</dt><dd>{detail.user_agent||'Not recorded'}</dd></div></dl></AuditSection>
      </>}
    </div>}
  </li>;
}
function AuditLog(){
  const [filters,setFilters]=useState<AuditFilters>(NO_AUDIT_FILTERS),[search,setSearch]=useState(''),[page,setPage]=useState(1);
  const [result,setResult]=useState<AuditPage|null>(null),[error,setError]=useState('');
  // Held separately from the filters so typing stays responsive; committed after
  // a pause so a sentence does not become a request per keystroke.
  useEffect(()=>{const timer=setTimeout(()=>setFilters(current=>current.q===search?current:{...current,q:search}),300);return()=>clearTimeout(timer);},[search]);
  useEffect(()=>{let alive=true;setResult(null);api<AuditPage>('/api/audit/'+auditQuery(filters,page)).then(d=>{if(alive){setResult(d);setError('');}}).catch(e=>{if(alive)setError(errorText(e));});return()=>{alive=false;};},[filters,page]);
  // Every filter goes through here, so resetting to the first page cannot be
  // forgotten on one of them and leave the reader stranded on an empty page.
  const change=(next:Partial<AuditFilters>)=>{setFilters(current=>({...current,...next}));setPage(1);};
  const clear=()=>{setFilters(NO_AUDIT_FILTERS);setSearch('');setPage(1);};
  // Drilling into a request is a filter like any other. Everything else is
  // dropped, because "what else happened in this request" only has an answer if
  // it is not still being narrowed by an unrelated category or date range.
  const drill=(requestId:string)=>{setSearch('');change({...NO_AUDIT_FILTERS,request_id:requestId});};
  const taxonomy=result?.taxonomy;
  const categoryLabel=(id:string)=>taxonomy?.categories.find(c=>c.id===id)?.label??id;
  const outcomeLabel=(id:string)=>taxonomy?.outcomes.find(o=>o.id===id)?.label??id;
  const active:Array<{key:string;kind:string;value:string;remove:()=>void}>=[];
  if(filters.category)active.push({key:'category',kind:'Category',value:categoryLabel(filters.category),remove:()=>change({category:''})});
  if(filters.outcome)active.push({key:'outcome',kind:'Outcome',value:outcomeLabel(filters.outcome),remove:()=>change({outcome:''})});
  if(filters.actor)active.push({key:'actor',kind:'Account',value:filters.actor,remove:()=>change({actor:''})});
  if(filters.since)active.push({key:'since',kind:'From',value:filters.since,remove:()=>change({since:''})});
  if(filters.until)active.push({key:'until',kind:'Until',value:filters.until,remove:()=>change({until:''})});
  if(filters.request_id)active.push({key:'request',kind:'Request',value:filters.request_id,remove:()=>change({request_id:''})});
  if(filters.security)active.push({key:'security',kind:'Showing',value:'security activity only',remove:()=>change({security:false})});
  if(filters.q)active.push({key:'q',kind:'Search',value:filters.q,remove:()=>setSearch('')});
  return <section className="panel">
    <div className="panel-heading"><div><h2>Audit history</h2><p>Every recorded change, newest first. Recorded entries cannot be edited or deleted through this app.</p></div></div>
    <div className="filters audit-filters">
      <div className="search-field"><Search size={17}/><input aria-label="Search audit history" type="search" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search an event, member, account or date"/></div>
      <select aria-label="Category" value={filters.category} onChange={e=>change({category:e.target.value})}><option value="">All categories</option>{taxonomy?.categories.map(c=><option key={c.id} value={c.id}>{c.label}</option>)}</select>
      <select aria-label="Outcome" value={filters.outcome} onChange={e=>change({outcome:e.target.value})}><option value="">Any outcome</option>{taxonomy?.outcomes.map(o=><option key={o.id} value={o.id}>{o.label}</option>)}</select>
      <select aria-label="Account" value={filters.actor} onChange={e=>change({actor:e.target.value})}><option value="">All accounts</option>{result?.actors.map(a=><option key={a.username} value={a.username}>{a.username} ({a.role})</option>)}</select>
      <label className="audit-date">From<input aria-label="From date" type="date" value={filters.since} onChange={e=>change({since:e.target.value})}/></label>
      <label className="audit-date">To<input aria-label="To date" type="date" value={filters.until} onChange={e=>change({until:e.target.value})}/></label>
      <label className="audit-toggle"><input type="checkbox" checked={filters.security} onChange={e=>change({security:e.target.checked})}/> Security activity only</label>
    </div>
    {active.length>0&&<div className="audit-chips">{active.map(chip=><span className="audit-chip-filter" key={chip.key}>{chip.kind}{chip.value&&<>: {chip.value}</>}<button type="button" onClick={chip.remove} aria-label={`Remove the ${chip.kind.toLowerCase()} filter`}><X size={13}/></button></span>)}<button type="button" className="text-button" onClick={clear}>Clear all</button></div>}
    {error?<Alert>{error} <button className="text-button" onClick={()=>setFilters({...filters})}>Retry</button></Alert>:!result?<div className="loading" role="status">Loading history…</div>:<>
      {result.events.length?<ul className="audit-list">{result.events.map(e=><AuditRow key={e.id} event={e} onDrill={drill}/>)}</ul>:active.length>0?<Empty>No events match these filters. Try widening or clearing them.</Empty>:<Empty>No audit events recorded yet.</Empty>}
      <Pager page={page} more={result.has_more} onChange={setPage}/>
    </>}
  </section>;
}
