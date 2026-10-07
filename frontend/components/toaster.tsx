'use client';
import { AnimatePresence, motion } from 'framer-motion';
import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react';

type ToastItem={id:number;message:string;kind:string};
export function Toaster({items,onDismiss}:{items:ToastItem[];onDismiss:(id:number)=>void}) {
 const Icon = ({kind}:{kind:string}) => kind==='error'?<CircleAlert size={17}/>:kind==='info'?<Info size={17}/>:<CheckCircle2 size={17}/>;
 return <div className="toast-stack" aria-live="polite" aria-atomic="false"><AnimatePresence>{items.map(t=><motion.div key={t.id} data-toast={t.id} className={`toast toast-${t.kind}`} initial={{opacity:0,y:14,scale:.98}} animate={{opacity:1,y:0,scale:1}} exit={{opacity:0,y:8}}><Icon kind={t.kind}/><span>{t.message}</span><button aria-label="Dismiss notification" onClick={()=>onDismiss(t.id)}><X size={14}/></button></motion.div>)}</AnimatePresence></div>;
}
