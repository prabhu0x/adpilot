'use client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createContext, useContext, useMemo, useState } from 'react';
import { Toaster } from './toaster';

const ToastCtx = createContext<(message: string, kind?: 'success'|'error'|'info') => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { staleTime: 20_000, retry: 1, refetchOnWindowFocus: false } } }));
  const [items, setItems] = useState<{id:number; message:string; kind:string}[]>([]);
  const toast = useMemo(() => (message: string, kind: 'success'|'error'|'info' = 'success') => {
    const id = Date.now() + Math.random(); setItems(old => [...old, {id, message, kind}]);
    setTimeout(() => setItems(old => old.filter(x => x.id !== id)), 3600);
  }, []);
  return <QueryClientProvider client={client}><ToastCtx.Provider value={toast}>{children}<Toaster items={items} onDismiss={id=>setItems(old=>old.filter(x=>x.id!==id))} /></ToastCtx.Provider></QueryClientProvider>;
}
