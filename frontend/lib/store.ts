'use client';
import { create } from 'zustand';

type UiState = {
  days: number; platform: string; collapsed: boolean; light: boolean; setDays:(n:number)=>void;
  setPlatform:(p:string)=>void; toggleCollapsed:()=>void; toggleLight:()=>void;
};
export const useUi = create<UiState>((set)=>({
  days:14, platform:'all', collapsed:false, light:false,
  setDays:(days)=>set({days}), setPlatform:(platform)=>set({platform}),
  toggleCollapsed:()=>set(s=>({collapsed:!s.collapsed})),
  toggleLight:()=>set(s=>({light:!s.light})),
}));
