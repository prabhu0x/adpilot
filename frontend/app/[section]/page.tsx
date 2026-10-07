import { notFound } from 'next/navigation';
import { Shell } from '@/components/shell';
import { WorkspaceView } from '@/components/workspace-view';
const sections=['command-center','data','explorer','diagnosis','opportunities','decisions','optimizer','inventory','creatives','execution','learning','copilot','settings','scenarios'];
export function generateStaticParams(){return sections.map(section=>({section}));}
export default async function SectionPage({params}:{params:Promise<{section:string}>}){const {section}=await params;if(!sections.includes(section))notFound();return <Shell section={section}><WorkspaceView section={section}/></Shell>;}
