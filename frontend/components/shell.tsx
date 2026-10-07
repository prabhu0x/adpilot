'use client';
import { ReactNode, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell, Boxes, BrainCircuit, ChartNoAxesCombined, ChevronDown, Command, Compass, Database, FileBarChart, Gauge, Layers3, LogOut, Menu, Moon, Orbit, PanelLeftClose, PanelLeftOpen, Search, Settings2, ShieldAlert, ShieldCheck, Sparkles, Sun, Target, TrendingUp, TriangleAlert, Zap } from 'lucide-react';
import { API_ORIGIN, clearTokens, get, post, put } from '@/lib/api';
import { useUi } from '@/lib/store';
import { useToast } from './providers';

const nav=[
 {key:'command-center',label:'Command center',group:'WORKSPACE',icon:Gauge},
 {key:'data',label:'Unified data hub',group:'WORKSPACE',icon:Database},
 {key:'explorer',label:'Performance explorer',group:'INTELLIGENCE',icon:ChartNoAxesCombined},
 {key:'diagnosis',label:'Anomaly & diagnosis',group:'INTELLIGENCE',icon:TriangleAlert},
 {key:'opportunities',label:'Opportunity radar',group:'INTELLIGENCE',icon:Compass},
 {key:'decisions',label:'Decision center',group:'DECISIONING',icon:Target},
 {key:'optimizer',label:'Budget optimizer',group:'DECISIONING',icon:TrendingUp},
 {key:'inventory',label:'Inventory & margin',group:'DECISIONING',icon:Boxes},
 {key:'creatives',label:'Creative intelligence',group:'DECISIONING',icon:Layers3},
 {key:'execution',label:'Execution & audit',group:'GOVERNANCE',icon:ShieldCheck},
 {key:'learning',label:'Learning loop',group:'GOVERNANCE',icon:BrainCircuit},
 {key:'copilot',label:'AI copilot',group:'GOVERNANCE',icon:Sparkles},
 {key:'settings',label:'Settings',group:'SYSTEM',icon:Settings2},
 {key:'scenarios',label:'Scenario guide',group:'SYSTEM',icon:FileBarChart},
];

export function Shell({children,section}:{children:ReactNode;section:string}){
 const path=usePathname(),router=useRouter(),toast=useToast(),qc=useQueryClient();
 const {days,setDays,platform,setPlatform,collapsed,toggleCollapsed,light,toggleLight}=useUi();
 const [ready,setReady]=useState(false),[mobileNav,setMobileNav]=useState(false),[palette,setPalette]=useState(false),[query,setQuery]=useState(''),[notifs,setNotifs]=useState(false),[userMenu,setUserMenu]=useState(false),[busy,setBusy]=useState(false);
 const token=typeof window!=='undefined'?localStorage.getItem('adpilot.access'):null;
 const user=useQuery({queryKey:['me'],queryFn:()=>get<any>('/auth/me'),enabled:!!token});
 const notices=useQuery({queryKey:['notifications'],queryFn:()=>get<any>('/notifications?unread_only=false&limit=15'),enabled:!!token,refetchInterval:60000});
 const sim=useQuery({queryKey:['simulation'],queryFn:()=>get<any>('/simulation'),enabled:!!token,refetchInterval:60000});
 // Read the browser-stored auth token only after hydration to avoid SSR/client markup mismatch.
 // eslint-disable-next-line react-hooks/set-state-in-effect
 useEffect(()=>{setReady(true);},[]);
 useEffect(()=>{document.body.dataset.theme=light?'light':'dark';},[light]);
 useEffect(()=>{if(ready&&!token)router.replace('/login');},[ready,token,router]);
 useEffect(()=>{
  const handler=(e:KeyboardEvent)=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();setPalette(v=>!v);}if(e.key==='Escape'){setPalette(false);setMobileNav(false);}};
  window.addEventListener('keydown',handler);return()=>window.removeEventListener('keydown',handler);
 },[]);
 useEffect(()=>{
  if(!token)return;
  const source=new EventSource(`${API_ORIGIN}/api/v1/events`);
  const refresh=()=>{qc.invalidateQueries({queryKey:['overview']});qc.invalidateQueries({queryKey:['recommendations']});qc.invalidateQueries({queryKey:['anomalies']});qc.invalidateQueries({queryKey:['simulation']});};
  ['pipeline','recommendation','execution','simulation','upload','notification'].forEach(name=>source.addEventListener(name,refresh));
  return()=>source.close();
 },[token,qc]);
 const active=nav.find(n=>n.key===section)||nav[0];
 const grouped=useMemo(()=>nav.reduce<Record<string,typeof nav>>((a,n)=>{(a[n.group]??=[]).push(n);return a;},{}),[]);
 const matching=nav.filter(n=>`${n.label} ${n.group}`.toLowerCase().includes(query.toLowerCase()));
 async function sync(){setBusy(true);try{const res=await post<any>('/sources/sync?source_key=all');toast(`Sync finished · ${res.rows_ingested??res.total_rows??'source'} rows processed`);await qc.invalidateQueries();}catch(e:any){toast(e.message,'error');}finally{setBusy(false);}}
 async function toggleKill(){const on=!!sim.data?.kill_switch;try{await put('/settings/kill-switch',{enabled:!on});await qc.invalidateQueries({queryKey:['simulation']});await qc.invalidateQueries({queryKey:['settings']});toast(!on?'Global kill switch enabled':'Global kill switch released',!on?'error':'success');}catch(e:any){toast(e.message,'error');}}
 function signout(){clearTokens();router.push('/login');}
 if(!ready||(!token&&ready))return <div className="boot-screen"><span className="brand-mark"><Orbit size={22}/></span><span>Preparing your workspace…</span></div>;
 return <div className={`app-shell ${collapsed?'nav-collapsed':''}`}>
  <aside className={`sidebar ${mobileNav?'mobile-open':''}`}>
   <div className="sidebar-head"><Link href="/command-center" className="brand-lockup"><span className="brand-mark"><Orbit size={18}/></span>{!collapsed&&<><span className="brand-name">ADPILOT</span><span className="brand-beta">LAB</span></>}</Link><button className="icon-btn collapse-btn" aria-label={collapsed?'Expand navigation':'Collapse navigation'} onClick={toggleCollapsed}>{collapsed?<PanelLeftOpen size={16}/>:<PanelLeftClose size={16}/>}</button></div>
   {!collapsed&&<button className="brand-select"><span className="brand-avatar">L</span><span className="brand-select-copy"><b>Lumière Skin Co.</b><small>Direct-to-consumer</small></span><ChevronDown size={14}/></button>}
   {collapsed&&<button className="brand-avatar brand-avatar-alone" aria-label="Lumière Skin Co.">L</button>}
   <button className="search-trigger" onClick={()=>setPalette(true)}><Search size={15}/>{!collapsed&&<><span>Quick search</span><kbd>⌘ K</kbd></>}</button>
   <nav className="nav-list" aria-label="Main navigation">{Object.entries(grouped).map(([group,items])=><div className="nav-group" key={group}>{!collapsed&&<div className="nav-group-label">{group}</div>}{items.map(item=>{const Icon=item.icon;return <Link key={item.key} href={`/${item.key}`} onClick={()=>setMobileNav(false)} className={`nav-item ${section===item.key?'active':''}`} title={collapsed?item.label:undefined}><Icon size={17}/>{!collapsed&&<span>{item.label}</span>}{item.key==='copilot'&&!collapsed&&<i className="nav-ai-dot"/>}</Link>;})}</div>)}</nav>
   {!collapsed&&<div className="sidebar-bottom"><div className="sim-status"><span className="live-dot"/><span>SIMULATOR ACTIVE</span><span className="sim-date">{sim.data?.clock_date||'—'}</span></div><div className="sidebar-bottom-copy"><span className="mini-mark">◌</span><span>Profit over platform ROAS.</span></div></div>}
  </aside>
  {mobileNav&&<button className="nav-scrim" aria-label="Close menu" onClick={()=>setMobileNav(false)}/>}
  <div className="main-column">
   <header className="topbar"><div className="topbar-left"><button className="icon-btn mobile-menu" aria-label="Open navigation" onClick={()=>setMobileNav(true)}><Menu size={19}/></button><div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><b>{active.label}</b></div></div>
    <div className="topbar-actions"><div className="date-select"><span className="topbar-label">PERIOD</span><select value={days} onChange={e=>setDays(Number(e.target.value))} aria-label="Reporting period"><option value={7}>Last 7 days</option><option value={14}>Last 14 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option></select></div><div className="platform-select"><span className="topbar-label">CHANNEL</span><select value={platform} onChange={e=>setPlatform(e.target.value)} aria-label="Filter platform"><option value="all">All channels</option><option value="meta">Meta</option><option value="google">Google</option><option value="amazon">Amazon</option><option value="tiktok">TikTok</option><option value="programmatic">Programmatic</option></select></div>
     <button className={`btn btn-sync ${busy?'is-loading':''}`} onClick={sync} disabled={busy} title="Sync data sources"><Zap size={14}/><span>{busy?'Syncing':'Sync now'}</span></button>
     <button className={`icon-btn kill-switch ${sim.data?.kill_switch?'kill-on':''}`} onClick={toggleKill} title={sim.data?.kill_switch?'Disable global kill switch':'Enable global kill switch'} aria-label="Toggle kill switch"><ShieldAlert size={17}/></button>
     <div className="topbar-pop-wrap"><button className="icon-btn notification-btn" onClick={()=>setNotifs(v=>!v)} aria-label="Notifications"><Bell size={17}/>{(notices.data?.unread||0)>0&&<i className="notification-dot"/>}</button>{notifs&&<div className="popover notifications-pop"><div className="popover-title"><span>Notifications</span><button className="text-btn" onClick={async()=>{await post('/notifications/read-all');await qc.invalidateQueries({queryKey:['notifications']});}}>Mark all read</button></div>{(notices.data?.items||[]).length?(notices.data.items.slice(0,7).map((n:any)=><button key={n.id} className={`notice-item ${n.read?'':'unread'}`} onClick={async()=>{await post(`/notifications/${n.id}/read`);await qc.invalidateQueries({queryKey:['notifications']});}}><span className="notice-dot"/><span><b>{n.title}</b><small>{n.body}</small></span></button>)):<div className="empty-mini">You&apos;re all caught up.</div>}</div>}</div>
     <button className="icon-btn theme-toggle" onClick={toggleLight} aria-label={light?'Switch to dark theme':'Switch to light theme'}>{light?<Moon size={17}/>:<Sun size={17}/>}</button>
     <div className="topbar-pop-wrap"><button className="user-chip" onClick={()=>setUserMenu(v=>!v)}><span className="avatar">{user.data?.avatar_initials||'AA'}</span><span className="user-chip-name">{user.data?.full_name||'AdPilot user'}</span><ChevronDown size={13}/></button>{userMenu&&<div className="popover user-pop"><div className="user-pop-head"><b>{user.data?.full_name}</b><small>{user.data?.role_label||'Admin'} · {user.data?.email}</small></div><button onClick={signout}><LogOut size={14}/> Sign out</button></div>}</div>
    </div>
   </header>
   <main className="page-content" key={path}>{children}</main>
   <footer className="app-footer"><span>AdPilot decision intelligence <i>·</i> demo workspace</span><span><span className="live-dot"/> LOCAL SIMULATION</span></footer>
  </div>
  {palette&&<div className="modal-backdrop" onMouseDown={()=>setPalette(false)}><div className="command-modal" onMouseDown={e=>e.stopPropagation()}><div className="command-input"><Search size={17}/><input autoFocus placeholder="Search pages and actions…" value={query} onChange={e=>setQuery(e.target.value)}/><kbd>ESC</kbd></div><div className="command-results"><div className="command-group-label">NAVIGATION</div>{matching.map((item,i)=>{const Icon=item.icon;return <Link href={`/${item.key}`} key={item.key} onClick={()=>setPalette(false)} className="command-row"><Icon size={16}/><span>{item.label}</span><kbd>{i<9?`⌘ ${i+1}`:''}</kbd></Link>;})}{matching.length===0&&<div className="empty-mini">No matching pages.</div>}<button className="command-row" onClick={()=>{setPalette(false);sync();}}><Zap size={16}/><span>Sync all data sources</span><kbd>↵</kbd></button></div><div className="command-foot"><span><Command size={13}/> Search instantly across your workspace</span><span>↑↓ navigate · ↵ open</span></div></div></div>}
 </div>;
}
