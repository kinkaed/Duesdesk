export type Role='secretary'|'auditor'|'member';
export type Session={username:string;role:Role;user_id:number;today:string;demo:boolean;organisation:string};
export type Member={id:number;code:string;name:string;phone:string;email:string;joined:string;billing_end:string;member_status:string;paid:string;balance:string;arrears:string;status:string};
export type Payment={id:number;receipt:string;member_id:number;member:string;amount:string;date:string;method:string;reference:string;notes:string;voided:boolean;void_reason:string;allocations:{month:string;amount:string}[]};
export type Overview={members:Member[];metrics:{collections:string;assigned:string;outstanding:string;arrears:string;paid:number;active:number};recent:Payment[]};
export type MemberProfile=Member&{total:string;payments:Payment[]};
export type Allocation={month:string;amount:string;status:string};
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
