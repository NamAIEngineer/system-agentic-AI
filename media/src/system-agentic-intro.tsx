import React from 'react';
import {AbsoluteFill, Composition, Sequence, interpolate, spring, useCurrentFrame, useVideoConfig, registerRoot} from 'remotion';

const C = {bg: '#07111f', bg2: '#0d2035', ink: '#f4f8ff', muted: '#9bb0c6', cyan: '#72e6d2', violet: '#aeb9ff', orange: '#ffb86b', line: '#294560', green: '#84e1ad', red: '#ff9a9a'};
const FONT = 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
const clamp = (v:number) => Math.max(0, Math.min(1, v));
const ease = (v:number) => { v = clamp(v); return v < .5 ? 4 * v * v * v : 1 - Math.pow(-2 * v + 2, 3) / 2; };
const enter = (frame:number, start:number, duration=28) => ease((frame - start) / duration);
const fmt = (n:number) => String(Math.max(0, Math.min(100, Math.round(n)))).padStart(3, '0');

function Fade({children, start=0, y=22, duration=28}:{children:React.ReactNode;start?:number;y?:number;duration?:number}) {
  const frame = useCurrentFrame(); const p = enter(frame, start, duration);
  return <div style={{opacity:p, transform:`translateY(${(1-p)*y}px)`}}>{children}</div>;
}

function Kicker({children, color=C.cyan}:{children:React.ReactNode;color?:string}) {
  return <div style={{fontSize:16, letterSpacing:3.3, textTransform:'uppercase', color, fontWeight:800}}>{children}</div>;
}

function Header({section}:{section:string}) {
  return <div style={{position:'absolute',left:70,right:70,top:44,display:'flex',justifyContent:'space-between',alignItems:'center',fontSize:17,color:C.muted}}>
    <div style={{fontWeight:800,color:C.ink,letterSpacing:-.3}}>SYSTEM AGENTIC AI</div>
    <div style={{display:'flex',gap:28,alignItems:'center'}}><span>{section}</span><span style={{width:8,height:8,borderRadius:99,background:C.cyan,boxShadow:`0 0 18px ${C.cyan}`}} /></div>
  </div>;
}

function Footer({source=true}:{source?:boolean}) {
  return <div style={{position:'absolute',left:70,right:70,bottom:38,display:'flex',justifyContent:'space-between',fontSize:14,color:'#71869d',letterSpacing:.4}}>
    <span>{source ? 'PUBLIC REPO · 7b7168a · EVIDENCE-LED' : 'NamAIEngineer · AI Product Engineer'}</span><span>{source ? 'NO SECRETS · NO EXTERNAL WRITE' : 'github.com/NamAIEngineer/system-agentic-AI'}</span>
  </div>;
}

function DotGrid() {
  return <div style={{position:'absolute',inset:0,opacity:.23,backgroundImage:'radial-gradient(#6f9aba 1px, transparent 1px)',backgroundSize:'28px 28px',maskImage:'linear-gradient(90deg,transparent,black 18%,black 82%,transparent)'}} />;
}

function Panel({children, x, y, w, h, border=C.line, fill='rgba(12,31,53,.88)', style={}}:{children:React.ReactNode;x:number;y:number;w:number;h:number;border?:string;fill?:string;style?:React.CSSProperties}) {
  return <div style={{position:'absolute',left:x,top:y,width:w,height:h,border:`1px solid ${border}`,borderRadius:20,background:fill,boxShadow:'0 24px 70px rgba(0,0,0,.26)',overflow:'hidden',...style}}>{children}</div>;
}

function Pill({children,color=C.cyan}:{children:React.ReactNode;color?:string}) {
  return <span style={{display:'inline-flex',alignItems:'center',gap:8,border:`1px solid ${color}66`,background:`${color}12`,borderRadius:99,padding:'8px 14px',fontSize:15,color}}>{children}</span>;
}

function TerminalLine({children, color=C.ink, start=0}:{children:React.ReactNode;color?:string;start?:number}) {
  return <Fade start={start} y={10}><div style={{fontFamily:'ui-monospace, SFMono-Regular, Menlo, monospace',fontSize:18,lineHeight:1.55,color,whiteSpace:'nowrap'}}>{children}</div></Fade>;
}

function Connector({x1,y1,x2,y2,start=0,color=C.cyan}:{x1:number;y1:number;x2:number;y2:number;start?:number;color?:string}) {
  const frame = useCurrentFrame(); const p = enter(frame, start, 32); const dx = x2-x1; const dy = y2-y1; const len = Math.hypot(dx,dy); const angle = Math.atan2(dy,dx) * 180 / Math.PI;
  return <div style={{position:'absolute',left:x1,top:y1,width:len,height:2,background:`linear-gradient(90deg,transparent,${color},transparent)`,transformOrigin:'left center',transform:`rotate(${angle}deg) scaleX(${p})`,opacity:p*.85}} />;
}

function SceneHook() {
  const frame = useCurrentFrame();
  return <AbsoluteFill style={{background:`radial-gradient(circle at 80% 22%, #1d4a5b 0, transparent 38%), linear-gradient(140deg,${C.bg},${C.bg2})`,fontFamily:FONT,color:C.ink}}><DotGrid/><Header section="INTRODUCTION"/><Fade start={18}><div style={{position:'absolute',left:90,top:220,maxWidth:1020}}><Kicker>Provider-neutral execution</Kicker><div style={{fontSize:108,lineHeight:.94,letterSpacing:-6,fontWeight:850,marginTop:24}}>Make the<br/><span style={{color:C.cyan}}>boundary</span> visible.</div><div style={{marginTop:34,fontSize:27,color:C.muted,maxWidth:820,lineHeight:1.35}}>A practical operating layer for AI workspaces: route the request, bound the action, verify the result.</div><div style={{display:'flex',gap:12,marginTop:34}}><Pill>real CLI evidence</Pill><Pill color={C.violet}>local-first by default</Pill></div></div></Fade><div style={{position:'absolute',right:120,bottom:160,width:270,height:270,border:`1px solid ${C.cyan}55`,borderRadius:'50%',opacity:enter(frame,32,42),transform:`scale(${.78+.22*enter(frame,32,42)})`}}><div style={{position:'absolute',inset:25,border:`1px solid ${C.violet}55`,borderRadius:'50%'}}/><div style={{position:'absolute',left:'50%',top:18,width:2,height:100,background:`linear-gradient(${C.cyan},transparent)`,transformOrigin:'bottom',transform:`rotate(${frame/5}deg)`}}/></div><Footer/></AbsoluteFill>;
}

function ScenePrompt() {
  const frame = useCurrentFrame(); const local = frame;
  const prompt = 'Inspect provider readiness. Report the safe result. Do not write files or call external services.';
  const chars = Math.floor(clamp((local-34)/150) * prompt.length); const typed = prompt.slice(0, chars);
  return <AbsoluteFill style={{background:C.bg,fontFamily:FONT,color:C.ink}}><DotGrid/><Header section="01 / SAFE PROMPT"/><Fade start={18}><div style={{position:'absolute',left:90,top:165}}><Kicker color={C.orange}>A request with a trust boundary</Kicker><div style={{fontSize:58,fontWeight:800,letterSpacing:-2,marginTop:16}}>Start with a safe prompt.</div></div></Fade><Panel x={90} y={310} w={1740} h={455} border={C.orange+'88'} fill="rgba(8,20,34,.96)"><div style={{height:52,padding:'0 22px',display:'flex',alignItems:'center',gap:10,borderBottom:`1px solid ${C.line}`,color:C.muted,fontFamily:'ui-monospace,monospace',fontSize:15}}><span style={{width:10,height:10,borderRadius:'50%',background:'#ff7161'}}/><span style={{width:10,height:10,borderRadius:'50%',background:'#ffbd44'}}/><span style={{width:10,height:10,borderRadius:'50%',background:'#00c853'}}/><span style={{marginLeft:14}}>orchestrator · read-only task</span></div><div style={{padding:'45px 52px'}}><div style={{color:C.muted,fontFamily:'ui-monospace,monospace',fontSize:18,marginBottom:20}}><span style={{color:C.cyan}}>nam@workspace</span><span style={{color:C.muted}}>:</span><span style={{color:C.violet}}>~</span><span style={{color:C.muted}}>$</span> task.submit</div><div style={{fontFamily:'ui-monospace,monospace',fontSize:30,lineHeight:1.45,color:C.ink,maxWidth:1480}}>{typed}<span style={{display:'inline-block',width:3,height:32,background:C.orange,marginLeft:4,verticalAlign:-3,opacity:Math.sin(frame/7)>0?1:.2}} /></div><div style={{display:'flex',gap:14,marginTop:48}}><Pill color={C.green}>READ_ONLY</Pill><Pill color={C.violet}>no external write</Pill><Pill color={C.orange}>bounded evidence</Pill></div></div></Panel><Footer/></AbsoluteFill>;
}

function SceneRouting() {
  const frame = useCurrentFrame();
  return <AbsoluteFill style={{background:`linear-gradient(145deg,${C.bg},#0a1828)`,fontFamily:FONT,color:C.ink}}><DotGrid/><Header section="02 / DECISION PLANE"/><Fade start={12}><div style={{position:'absolute',left:90,top:155}}><Kicker>Observed output · public commit 7b7168a</Kicker><div style={{fontSize:56,fontWeight:800,letterSpacing:-2,marginTop:15}}>Route first. Execute second.</div></div></Fade><Panel x={90} y={300} w={500} h={380} border={C.line}><div style={{padding:28}}><Pill color={C.orange}>PROMPT</Pill><div style={{fontSize:25,fontWeight:700,lineHeight:1.25,marginTop:25}}>Inspect provider readiness.</div><div style={{fontFamily:'ui-monospace,monospace',fontSize:16,color:C.muted,marginTop:22,lineHeight:1.65}}>safe_prompt: true<br/>external_write: false<br/>workspace: public clone</div></div></Panel><Connector x1={590} y1={492} x2={750} y2={492} start={22}/><Panel x={750} y={255} w={350} h={475} border={C.cyan+'99'} fill="rgba(14,43,62,.94)"><div style={{padding:28}}><Pill>DECISION PLANE</Pill><div style={{fontSize:21,fontWeight:700,marginTop:26}}>Quyet Tiny</div><div style={{fontFamily:'ui-monospace,monospace',fontSize:17,lineHeight:1.8,color:C.muted,marginTop:20}}>route<br/><strong style={{color:C.cyan,fontSize:25}}>DISPATCH</strong><br/><span style={{color:C.violet}}>module</span><br/><strong>none</strong></div><div style={{marginTop:32}}><Pill color={C.orange}>advisory only</Pill></div></div></Panel><Connector x1={1100} y1={492} x2={1260} y2={492} start={54}/><Panel x={1260} y={300} w={570} h={380} border={C.violet+'88'}><div style={{padding:28}}><Pill color={C.violet}>GATE</Pill><div style={{fontSize:24,fontWeight:750,marginTop:25}}>Execution is still host-owned.</div><div style={{fontFamily:'ui-monospace,monospace',fontSize:18,lineHeight:1.75,marginTop:22,color:C.muted}}><span style={{color:C.orange}}>&quot;execution_authorized&quot;</span>: <strong style={{color:C.orange}}>false</strong><br/><span style={{color:C.orange}}>&quot;route_lane&quot;</span>: <strong>HOST</strong><br/><span style={{color:C.orange}}>&quot;confidence&quot;</span>: <strong>0.8357</strong></div></div></Panel><div style={{position:'absolute',left:760,top:754,fontSize:14,letterSpacing:2,color:C.muted}}>ILLUSTRATIVE FLOW · JSON VALUES ARE INSPECTED CLI OUTPUT</div><Footer/></AbsoluteFill>;
}

function SceneProviders() {
  const frame = useCurrentFrame(); const cards=[['CODEX / OPENAI',C.cyan,'READY','authenticated · healthy'],['LOCAL QWEN3',C.violet,'READY','loopback runtime · healthy'],['REA',C.orange,'UNAVAILABLE','not selected for this task']];
  return <AbsoluteFill style={{background:C.bg,fontFamily:FONT,color:C.ink}}><DotGrid/><Header section="03 / PROVIDER REGISTRY"/><Fade start={10}><div style={{position:'absolute',left:90,top:155}}><Kicker>Current live inventory</Kicker><div style={{fontSize:56,fontWeight:800,letterSpacing:-2,marginTop:15}}>Know what can run.</div><div style={{fontSize:23,color:C.muted,marginTop:18}}>Provider identity, health, and execution readiness stay separate.</div></div></Fade><div style={{position:'absolute',left:90,top:320,display:'flex',gap:22}}>{cards.map(([name,color,status,detail],i)=><Fade key={name} start={35+i*18} y={28}><Panel x={0} y={0} w={520} h={270} border={color+'88'} fill="rgba(12,30,50,.94)" style={{position:'relative',left:'auto',top:'auto'}}><div style={{padding:30}}><div style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}><Kicker color={color}>{name}</Kicker><span style={{width:12,height:12,borderRadius:'50%',background:color,boxShadow:`0 0 16px ${color}`}}/></div><div style={{fontSize:42,fontWeight:800,marginTop:26,color}}>{status}</div><div style={{fontFamily:'ui-monospace,monospace',fontSize:17,color:C.muted,marginTop:17}}>{detail}</div></div></Panel></Fade>)}</div><Panel x={90} y={650} w={1740} h={145} border={C.line} fill="rgba(6,18,31,.78)"><div style={{padding:24,display:'flex',justifyContent:'space-between',alignItems:'center'}}><div><div style={{fontFamily:'ui-monospace,monospace',fontSize:17,color:C.cyan}}>python3 scripts/system-agentic providers</div><div style={{fontSize:16,color:C.muted,marginTop:8}}>live readiness probe · no credentials shown</div></div><div style={{fontFamily:'ui-monospace,monospace',fontSize:16,color:C.green}}>runtime.queue_worker_loaded: true</div></div></Panel><Footer/></AbsoluteFill>;
}

function SceneBounded() {
  const frame = useCurrentFrame(); const bar = enter(frame, 30, 90); const rows=[['task execution','not run',C.orange],['external write','not attempted',C.green],['decision owner','host',C.violet],['policy behavior','fail closed',C.cyan]];
  return <AbsoluteFill style={{background:`radial-gradient(circle at 22% 75%,#153d4b 0,transparent 36%),${C.bg}`,fontFamily:FONT,color:C.ink}}><DotGrid/><Header section="04 / BOUNDED EXECUTION"/><Fade start={10}><div style={{position:'absolute',left:90,top:155}}><Kicker color={C.green}>The decision stops at its boundary</Kicker><div style={{fontSize:56,fontWeight:800,letterSpacing:-2,marginTop:15}}>Bound the action.</div></div></Fade><Panel x={90} y={315} w={760} h={425} border={C.green+'88'}><div style={{padding:34}}><Pill color={C.green}>INSPECTED CLI OUTPUT</Pill><div style={{fontFamily:'ui-monospace,monospace',fontSize:22,lineHeight:1.7,marginTop:28}}><div><span style={{color:C.muted}}>route</span> = <span style={{color:C.cyan}}>DISPATCH</span></div><div><span style={{color:C.muted}}>execution_class</span> = <span style={{color:C.green}}>READ_ONLY</span></div><div><span style={{color:C.muted}}>execution_authorized</span> = <span style={{color:C.orange}}>false</span></div></div><div style={{height:10,borderRadius:99,background:C.line,marginTop:40,overflow:'hidden'}}><div style={{height:'100%',width:`${bar*100}%`,background:`linear-gradient(90deg,${C.cyan},${C.green})`,boxShadow:`0 0 24px ${C.cyan}`}}/></div><div style={{display:'flex',justifyContent:'space-between',marginTop:12,fontSize:14,color:C.muted}}><span>authorization gate</span><span>{fmt(bar*100)}%</span></div></div></Panel><div style={{position:'absolute',left:970,top:315,width:720}}>{rows.map(([label,value,color],i)=><Fade key={label} start={30+i*18} y={14}><div style={{display:'flex',alignItems:'center',justifyContent:'space-between',padding:'20px 0',borderBottom:`1px solid ${C.line}`,fontFamily:'ui-monospace,monospace',fontSize:20}}><span style={{color:C.muted}}>{label}</span><span style={{color}}>{value}</span></div></Fade>)}</div><div style={{position:'absolute',right:90,bottom:128}}><Pill color={C.green}>ILLUSTRATIVE POLICY VIEW</Pill></div><Footer/></AbsoluteFill>;
}

function SceneVerify() {
  const frame = useCurrentFrame(); const check = (start:number) => enter(frame,start,28);
  const items=[['provider inventory','inspected',C.green,33],['route decision','advisory · host-owned',C.cyan,58],['source pin','7b7168a',C.violet,83],['external write','not attempted',C.orange,108]];
  return <AbsoluteFill style={{background:C.bg,fontFamily:FONT,color:C.ink}}><DotGrid/><Header section="05 / VERIFICATION"/><Fade start={10}><div style={{position:'absolute',left:90,top:155}}><Kicker color={C.violet}>Evidence before certainty</Kicker><div style={{fontSize:56,fontWeight:800,letterSpacing:-2,marginTop:15}}>Leave a receipt.</div></div></Fade><Panel x={90} y={300} w={1740} h={465} border={C.violet+'88'} fill="rgba(11,27,47,.96)"><div style={{padding:34}}><div style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}><Pill color={C.violet}>VERIFICATION RECEIPT</Pill><span style={{fontFamily:'ui-monospace,monospace',fontSize:16,color:C.muted}}>status: complete for inspected scope</span></div><div style={{marginTop:18}}>{items.map(([label,value,color,start])=><div key={label as string} style={{display:'flex',alignItems:'center',gap:22,padding:'20px 0',borderBottom:`1px solid ${C.line}`,fontFamily:'ui-monospace,monospace',fontSize:21,opacity:check(start as number),transform:`translateX(${(1-check(start as number))*18}px)`}}><span style={{width:18,height:18,borderRadius:5,border:`2px solid ${color}`,display:'inline-flex',alignItems:'center',justifyContent:'center',color}}>{check(start as number)>.9?'✓':''}</span><span style={{color:C.muted,width:320}}>{label}</span><span style={{color}}>{value}</span></div>)}</div><div style={{display:'flex',gap:13,marginTop:24}}><Pill color={C.green}>FACT</Pill><Pill color={C.orange}>UNKNOWN stays visible</Pill><Pill color={C.cyan}>reproducible path</Pill></div></div></Panel><Footer/></AbsoluteFill>;
}

function SceneEnd() {
  const frame = useCurrentFrame(); const p=enter(frame,10,42);
  return <AbsoluteFill style={{background:`radial-gradient(circle at 50% 35%,#184858 0,transparent 44%),linear-gradient(145deg,${C.bg},${C.bg2})`,fontFamily:FONT,color:C.ink}}><DotGrid/><div style={{position:'absolute',inset:0,display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',textAlign:'center',opacity:p,transform:`translateY(${(1-p)*28}px)`}}><Kicker>NamAIEngineer · AI Product Engineer</Kicker><div style={{fontSize:92,letterSpacing:-4,fontWeight:850,marginTop:25}}>System Agentic AI</div><div style={{fontSize:29,color:C.muted,marginTop:25}}>Provider-neutral execution with evidence.</div><div style={{display:'flex',gap:13,marginTop:35}}><Pill>route</Pill><Pill color={C.violet}>bound</Pill><Pill color={C.orange}>verify</Pill></div><div style={{fontFamily:'ui-monospace,monospace',fontSize:18,color:C.cyan,marginTop:58}}>github.com/NamAIEngineer/system-agentic-AI</div></div><Footer source={false}/></AbsoluteFill>;
}

function Scene({kind}:{kind:string}) { if(kind==='hook') return <SceneHook/>; if(kind==='prompt') return <ScenePrompt/>; if(kind==='routing') return <SceneRouting/>; if(kind==='providers') return <SceneProviders/>; if(kind==='bounded') return <SceneBounded/>; if(kind==='verify') return <SceneVerify/>; return <SceneEnd/>; }

const scenes = [
  {kind:'hook', from:0, duration:300},
  {kind:'prompt', from:300, duration:480},
  {kind:'routing', from:780, duration:540},
  {kind:'providers', from:1320, duration:600},
  {kind:'bounded', from:1920, duration:600},
  {kind:'verify', from:2520, duration:480},
  {kind:'end', from:3000, duration:240},
];

export function SystemAgenticIntro() { return <AbsoluteFill style={{background:C.bg}}>{scenes.map((scene) => <Sequence key={scene.kind} from={scene.from} durationInFrames={scene.duration}><Scene kind={scene.kind}/></Sequence>)}</AbsoluteFill>; }
export const Root = () => <Composition id="SystemAgenticIntro" component={SystemAgenticIntro} width={1920} height={1080} fps={60} durationInFrames={3240}/>;
registerRoot(Root);
