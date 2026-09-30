export type Role='secretary'|'auditor';
export type Branding={id:number;public_id:string;name:string;logo_url:string;primary:string;secondary:string;accent:string;primary_text:string;secondary_text:string;accent_text:string};
export type Session={username:string;role:Role;user_id:number;today:string;demo:boolean;organisation:string;branding:Branding};
export type Member={id:number;code:string;name:string;phone:string;email:string;joined:string;billing_end:string;member_status:string;paid:string;balance:string;arrears:string;status:string};
export type Payment={id:number;receipt:string;member_id:number;member:string;amount:string;date:string;method:string;reference:string;notes:string;voided:boolean;void_reason:string;allocations:{month:string;amount:string}[]};
export type Overview={members:Member[];metrics:{collections:string;assigned:string;outstanding:string;arrears:string;paid:number;active:number};recent:Payment[]};
export type MemberProfile=Member&{total:string;payments:Payment[]};
export type Allocation={month:string;amount:string;status:string};

// --- Audit history -----------------------------------------------------------
// One row of the history. `label`, `category` and `severity` are described
// server-side from the action name so that the words on screen and the strings
// in the database are maintained in one place; an action the taxonomy does not
// know still arrives with a usable label rather than a blank.
export type AuditSeverity='routine'|'notice'|'warning'|'critical';
export type AuditEvent={id:number;date:string;actor:string;actor_role:string;action:string;label:string;category:string;severity:AuditSeverity;outcome:string;outcome_label:string;reason:string;reason_label:string;request_id:string;entity:string;entity_id:string;resource:string;related_count:number};
export type AuditChange={field:string;from:unknown;to:unknown};
// The detail response is the list row plus everything the list withholds, and
// the events that happened during the same request.
export type AuditDetail=AuditEvent&{details:Record<string,unknown>;changes:AuditChange[];related:AuditEvent[];http_method:string;path:string;ip_address:string|null;ip_is_peer:boolean;user_agent:string};
export type AuditOption={id:string;label:string};
export type AuditActionMeta={label:string;category:string;severity:AuditSeverity};
export type AuditTaxonomy={actions:Record<string,AuditActionMeta>;categories:AuditOption[];outcomes:AuditOption[];severities:AuditSeverity[]};
export type AuditActor={username:string;role:Role};
export type AuditPage={events:AuditEvent[];page:number;has_more:boolean;page_size:number;actors:AuditActor[];taxonomy:AuditTaxonomy;total?:number};
export const AUDIT_PAGE_SIZE=100;
export const money=(n:string|number)=>'GH₵'+Number(n).toLocaleString('en-GH',{minimumFractionDigits:2,maximumFractionDigits:2});
export const csrf=()=>decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('csrftoken='))?.split('=')[1]||'');
export async function api<T>(url:string,data?:unknown):Promise<T>{
 const response=await fetch(url,{credentials:'same-origin',...(data!==undefined?{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify(data)}:{})});
 if(response.status===401){location.assign('/login/');throw Error('Please sign in again.');}
 if(response.status===403)throw Error('You do not have permission for this action. Reload if your session has expired.');
 let result;try{result=await response.json();}catch{throw Error('The server could not complete this request. Please try again.');}
 if(!response.ok)throw Error(result.error||'Could not complete the request.');
 return result;
}
export const formData=(form:HTMLFormElement)=>Object.fromEntries(new FormData(form));
// Django's own LogoutView ends the session, so this only sends the request and
// reports whether it worked. Failures are raised rather than hidden, because
// redirecting to /login/ while the session is still live would only look like a
// successful sign out.
export async function signOut():Promise<void>{
 let response:Response;
 try{response=await fetch('/logout/',{method:'POST',credentials:'same-origin',headers:{'X-CSRFToken':csrf()}});}
 catch{throw Error('We could not reach the server to sign you out. Check your connection and try again.');}
 if(!response.ok)throw Error('Signing out did not complete. Please try again.');
 location.assign('/login/');
}
