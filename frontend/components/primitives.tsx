'use client';
import { ReactNode } from 'react';
import { ArrowDownRight, ArrowUpRight, Minus, RefreshCw } from 'lucide-react';
import { ApiError } from '@/lib/api';

export function PageHeading({eyebrow,title,description,actions}:{eyebrow:string;title:string;description:string;actions?:ReactNode}){
 return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{actions&&<div className="heading-actions">{actions}</div>}</div>;
}
export function Panel({title,subtitle,action,children,className=''}:{title?:string;subtitle?:string;action?:ReactNode;children:ReactNode;className?:string}){
 return <section className={`panel ${className}`}><>{title&&<div className="panel-head"><div><h3>{title}</h3>{subtitle&&<p>{subtitle}</p>}</div>{action&&<div className="panel-action">{action}</div>}</div>}<div className={title?'panel-body':''}>{children}</div></></section>;
}
export function Stat({label,value,change,foot,icon,accent=false}:{label:string;value:string;change?:number|null;foot?:string;icon?:ReactNode;accent?:boolean}){
 const positive=(change||0)>=0;
 return <div className={`stat-card ${accent?'stat-accent':''}`}><div className="stat-top"><span>{label}</span>{icon&&<span className="stat-icon">{icon}</span>}</div><div className="stat-value">{value}</div><div className="stat-bottom">{change!==undefined&&change!==null?<span className={`delta ${positive?'positive':'negative'}`}>{positive?<ArrowUpRight size={14}/>:<ArrowDownRight size={14}/>} {Math.abs(change).toFixed(1)}% <small>vs prev.</small></span>:<span className="delta neutral"><Minus size={14}/><small>{foot||'no comparison'}</small></span>}{foot&&<span className="stat-foot">{foot}</span>}</div></div>;
}
export function Tag({children,tone='neutral'}:{children:ReactNode;tone?:'neutral'|'green'|'amber'|'red'|'blue'|'violet'}){return <span className={`tag tag-${tone}`}>{children}</span>;}
export function Button({children,onClick,variant='secondary',disabled=false,className='',type='button'}:{children:ReactNode;onClick?:()=>void;variant?:'primary'|'secondary'|'ghost'|'danger';disabled?:boolean;className?:string;type?:'button'|'submit'}){return <button type={type} className={`btn btn-${variant} ${className}`} onClick={onClick} disabled={disabled}>{children}</button>;}
export function StateBox({loading,error,retry,children}:{loading?:boolean;error?:unknown;retry?:()=>void;children?:ReactNode}){
	 if(loading)return <div className="state-box"><span className="spinner"/><span>Loading live data…</span></div>;
 if(error){const message=error instanceof ApiError?error.message:(error as Error)?.message||'API request failed';return <div className="state-box state-error"><b>Couldn’t load this view</b><span>{message}</span>{retry&&<button className="btn btn-secondary btn-sm" onClick={retry}><RefreshCw size={13}/> Try again</button>}</div>;}
 return <>{children}</>;
}
export function Empty({title,description,action}:{title:string;description:string;action?:ReactNode}){return <div className="empty-state"><span className="empty-orbit">◌</span><b>{title}</b><p>{description}</p>{action}</div>;}
export function Money({value,compact=false}:{value:number;compact?:boolean}){
 if(!Number.isFinite(value))return <>—</>;
 return <>{new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:compact?0:2,notation:compact?'compact':'standard'}).format(value)}</>;
}
export function NumberValue({value,maximumFractionDigits=1}:{value:number;maximumFractionDigits?:number}){return <>{new Intl.NumberFormat('en-US',{maximumFractionDigits}).format(Number(value||0))}</>;}
export function Percent({value,digits=1}:{value:number;digits?:number}){return <>{Number(value||0).toFixed(digits)}%</>;}
export function Table({columns,rows,empty='No records found.'}:{columns:{key:string;label:string;render?:(row:any)=>ReactNode;align?:'right'}[];rows:any[];empty?:string}){
 if(!rows?.length)return <Empty title="Nothing here yet" description={empty}/>;
 return <div className="table-wrap"><table className="data-table"><thead><tr>{columns.map(c=><th key={c.key} className={c.align==='right'?'align-right':''}>{c.label}</th>)}</tr></thead><tbody>{rows.map((row,i)=><tr key={row.id??row.key??i}>{columns.map(c=><td key={c.key} className={c.align==='right'?'align-right':''}>{c.render?c.render(row):(row[c.key]??'—')}</td>)}</tr>)}</tbody></table></div>;
}
export function IconText({children}:{children:ReactNode}){return <span className="icon-text">{children}</span>;}
