'use client';
import { FormEvent, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowRight, Eye, EyeOff, Orbit, ShieldCheck, Sparkles } from 'lucide-react';
import { get, post, saveTokens } from '@/lib/api';
import { useToast } from '@/components/providers';

type DemoUser={email:string;full_name:string;role_label:string;demo_password:string};
export default function LoginPage(){
 const router=useRouter(), toast=useToast();
 const [email,setEmail]=useState('admin@adpilot.io'),[password,setPassword]=useState('AdPilot!2026');
 const [show,setShow]=useState(false),[busy,setBusy]=useState(false),[users,setUsers]=useState<DemoUser[]>([]);
 useEffect(()=>{get<{items:DemoUser[]}>('/auth/demo-users').then(d=>setUsers(d.items)).catch(()=>{});},[]);
 async function submit(e:FormEvent){e.preventDefault();setBusy(true);try{const data=await post<any>('/auth/login',{email,password});saveTokens(data.access_token,data.refresh_token);toast('Signed in. Your workspace is ready.');router.push('/command-center');}catch(err:any){toast(err.message||'Could not sign in','error');}finally{setBusy(false);}}
 return <main className="login-page">
  <section className="login-story">
   <div className="login-brand"><span className="brand-mark"><Orbit size={20}/></span><span>ADPILOT</span></div>
   <div className="story-copy"><div className="eyebrow"><span className="live-dot"/> THE DECISION ENGINE FOR D2C</div><h1>Make every<br/><em>ad dollar</em><br/>work harder.</h1><p>One clear view across spend, stock and margin. Decisions grounded in what actually happened.</p>
    <div className="story-metrics"><div><b>5</b><span>media channels</span></div><div><b>40</b><span>demo SKUs</span></div><div><b>∞</b><span>better decisions</span></div></div>
   </div>
   <div className="login-foot"><span>01 / 05</span><span>INGEST → REASON → DECIDE → LEARN</span></div>
  </section>
  <section className="login-form-wrap"><div className="login-form-card">
   <div className="mobile-brand"><span className="brand-mark"><Orbit size={20}/></span> ADPILOT</div>
   <div className="eyebrow muted">YOUR WORKSPACE</div><h2>Sign in to AdPilot</h2><p className="subtle">Continue to Lumière Skin Co.</p>
   <form onSubmit={submit} className="login-form">
    <label>Email address<input autoComplete="username" type="email" value={email} onChange={e=>setEmail(e.target.value)} placeholder="you@company.com" required/></label>
    <label>Password<div className="password-field"><input autoComplete="current-password" type={show?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} required/><button type="button" className="icon-btn" aria-label={show?'Hide password':'Show password'} onClick={()=>setShow(!show)}>{show?<EyeOff size={16}/>:<Eye size={16}/>}</button></div></label>
    <button className="btn btn-primary btn-wide" disabled={busy}>{busy?'Signing in…':'Sign in'}<ArrowRight size={16}/></button>
   </form>
   <div className="demo-box"><div className="demo-title"><Sparkles size={15}/> Demo access <span>LOCAL SIMULATOR</span></div>
    <div className="demo-accounts">{(users.length?users:[{email:'admin@adpilot.io',full_name:'Avery Admin',role_label:'Admin',demo_password:'AdPilot!2026'},{email:'brand@adpilot.io',full_name:'Bailey Brand',role_label:'Brand Manager',demo_password:'AdPilot!2026'},{email:'analyst@adpilot.io',full_name:'Casey Analyst',role_label:'Analyst',demo_password:'AdPilot!2026'},{email:'viewer@adpilot.io',full_name:'Dana Viewer',role_label:'Viewer',demo_password:'AdPilot!2026'}]).map(u=><button key={u.email} className="demo-user" onClick={()=>{setEmail(u.email);setPassword(u.demo_password);}}><span className="avatar avatar-sm">{u.full_name.split(' ').map(x=>x[0]).slice(0,2).join('')}</span><span><b>{u.full_name}</b><small>{u.role_label}</small></span><ArrowRight size={13}/></button>)}</div>
    <div className="demo-password"><ShieldCheck size={13}/> Seeded demo password <b>AdPilot!2026</b></div>
   </div>
   <div className="login-legal">Development simulator · No live ad accounts connected</div>
  </div></section>
 </main>;
}
