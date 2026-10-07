'use client';
import { Background, Controls, MiniMap, ReactFlow, ReactFlowProvider } from '@xyflow/react';
import '@xyflow/react/dist/style.css';

function Flow({nodes,edges}:{nodes:any[];edges:any[]}){
 const mapped=(nodes||[]).map(n=>({...n,position:n.position||{x:0,y:0},data:{...n.data,label:n.data?.label||n.id}}));
 return <div className="lineage-canvas"><ReactFlow nodes={mapped} edges={edges||[]} fitView fitViewOptions={{padding:.15}} nodesDraggable nodesConnectable={false} proOptions={{hideAttribution:true}}><Background color="var(--line-strong)" gap={18}/><MiniMap pannable zoomable/><Controls/></ReactFlow></div>;
}
export function LineageMap(props:{nodes?:any[];edges?:any[]}){return <ReactFlowProvider><Flow nodes={props.nodes||[]} edges={props.edges||[]}/></ReactFlowProvider>;}
