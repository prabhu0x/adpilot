import type { Metadata } from 'next';
import { Providers } from '@/components/providers';
import './globals.css';

export const metadata: Metadata = {
  title: 'AdPilot — Decision Intelligence',
  description: 'Unified D2C advertising intelligence and decision engine.',
};
export default function RootLayout({children}:{children:React.ReactNode}) {
  return <html lang="en"><body><Providers>{children}</Providers></body></html>;
}
