'use client';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get } from './api';
import { useUi } from './store';

export function useWorkspaceData(section:string){
 const {days,platform}=useUi();
 const on=(...pages:string[])=>pages.includes(section);
 const token=typeof window!=='undefined'&&!!localStorage.getItem('adpilot.access');
 const overview=useQuery({queryKey:['overview',days],queryFn:()=>get<any>(`/overview?days=${days}`),enabled:token&&on('command-center')});
 const sources=useQuery({queryKey:['sources'],queryFn:()=>get<any>('/sources'),enabled:token&&on('data')});
 const quality=useQuery({queryKey:['quality'],queryFn:()=>get<any>('/quality'),enabled:token&&on('data')});
 const lineage=useQuery({queryKey:['lineage'],queryFn:()=>get<any>('/lineage'),enabled:token&&on('data')});
 const reconciliation=useQuery({queryKey:['reconciliation'],queryFn:()=>get<any>('/reconciliation'),enabled:token&&on('data')});
 const performance=useQuery({queryKey:['performance',days,platform],queryFn:()=>get<any>(`/analytics/performance?days=${days}${platform!=='all'?`&platform_key=${platform}`:''}`),enabled:token&&on('explorer','optimizer')});
 const anomalies=useQuery({queryKey:['anomalies'],queryFn:()=>get<any>('/anomalies'),enabled:token&&on('diagnosis')});
 const opportunities=useQuery({queryKey:['opportunities'],queryFn:()=>get<any>('/opportunities'),enabled:token&&on('opportunities')});
 const recommendations=useQuery({queryKey:['recommendations'],queryFn:()=>get<any>('/recommendations'),enabled:token&&on('decisions','command-center')});
 const inventory=useQuery({queryKey:['inventory'],queryFn:()=>get<any>('/inventory'),enabled:token&&on('inventory')});
 const creatives=useQuery({queryKey:['creatives'],queryFn:()=>get<any>('/creatives'),enabled:token&&on('creatives')});
 const executions=useQuery({queryKey:['executions'],queryFn:()=>get<any>('/executions'),enabled:token&&on('execution')});
 const audit=useQuery({queryKey:['audit'],queryFn:()=>get<any>('/audit'),enabled:token&&on('execution')});
 const learning=useQuery({queryKey:['learning'],queryFn:()=>get<any>('/learning'),enabled:token&&on('learning')});
 const simulation=useQuery({queryKey:['simulation'],queryFn:()=>get<any>('/simulation'),enabled:token&&on('learning')});
 const settings=useQuery({queryKey:['settings'],queryFn:()=>get<any>('/settings'),enabled:token&&on('settings','optimizer')});
 const scenarios=useQuery({queryKey:['scenarios'],queryFn:()=>get<any>('/scenarios'),enabled:token&&on('scenarios')});
 return {overview,sources,quality,lineage,reconciliation,performance,anomalies,opportunities,recommendations,inventory,creatives,executions,audit,learning,simulation,settings,scenarios};
}
export function useRefreshWorkspace(){const qc=useQueryClient();return ()=>qc.invalidateQueries();}
