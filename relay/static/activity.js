'use strict';
const activity = (() => {
 const lines=[], locks=new Map(), owned=new WeakSet(), watching=new Map();
 let lastKey='', reader=null;
 let matches=[],matchIndex=-1;
 const query=document.getElementById('log-query');
 function log(message){
  const field=document.getElementById('activity-log');
  const bottom=field.scrollHeight-field.scrollTop-field.clientHeight<30;
  lines.push(new Date().toLocaleTimeString('ru-RU')+'  '+message);
  if(lines.length>1000)lines.splice(0,lines.length-1000);
  renderLog(false);if(bottom&&!query.value)field.scrollTop=field.scrollHeight;
 }
 function renderLog(reset){
  const field=document.getElementById('activity-log'),scroll=field.scrollTop,text=lines.join('\n');
  const fragment=document.createDocumentFragment();matches=[];
  if(query.value){
   const escaped=query.value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
   const pattern=new RegExp(escaped,'giu');let end=0;
   for(const match of text.matchAll(pattern)){
    fragment.append(document.createTextNode(text.slice(end,match.index)));
    const mark=document.createElement('mark');mark.textContent=match[0];fragment.append(mark);matches.push(mark);end=match.index+match[0].length;
   }
   fragment.append(document.createTextNode(text.slice(end)));
  }else fragment.append(document.createTextNode(text));
  field.replaceChildren(fragment);field.scrollTop=scroll;
  matchIndex=matches.length?Math.min(reset?0:Math.max(0,matchIndex),matches.length-1):-1;
  selectMatch(reset);
 }
 function selectMatch(reveal){
  matches.forEach((mark,i)=>mark.classList.toggle('current',i===matchIndex));
  document.getElementById('log-count').textContent=`${matchIndex+1} / ${matches.length}`;
  for(const id of ['log-prev','log-next'])document.getElementById(id).disabled=!matches.length;
  if(reveal&&matchIndex>=0){const field=document.getElementById('activity-log');field.scrollTop+=matches[matchIndex].getBoundingClientRect().top-field.getBoundingClientRect().top-field.clientHeight/2;}
 }
 function moveMatch(direction){if(matches.length){matchIndex=(matchIndex+direction+matches.length)%matches.length;selectMatch(true);}}
 query.addEventListener('input',()=>renderLog(true));
 query.addEventListener('keydown',event=>{
  if(event.key==='Enter'){event.preventDefault();moveMatch(event.shiftKey?-1:1);}
  if(event.key==='Escape'){query.value='';renderLog(true);document.getElementById('activity-log').focus();}
 });
 document.getElementById('log-prev').onclick=()=>moveMatch(-1);
 document.getElementById('log-next').onclick=()=>moveMatch(1);
 document.addEventListener('keydown',event=>{
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='f'&&!document.getElementById('activity-panel').hidden&&!document.querySelector('dialog[open]')){
   event.preventDefault();query.focus();query.select();
  }
 });
 renderLog(true);
 function key(button){return button.dataset.actionKey||button.id||
  (button.closest('[data-card-id]')?.dataset.cardId||button.closest('[data-client-id]')?.dataset.clientId||'')+'|'+button.textContent;}
 function sync(){
  for(const button of document.querySelectorAll('button')){
   if(locks.get(key(button))?.armed){
    if(!button.disabled){owned.add(button);button.disabled=true;}
    if(!button.hasAttribute('data-cooling'))button.setAttribute('data-cooling','');
   }else if(button.hasAttribute('data-cooling')){
    button.removeAttribute('data-cooling');if(owned.has(button)){button.disabled=false;owned.delete(button);}
   }
  }
 }
 function release(k,record){if(locks.get(k)!==record)return;locks.delete(k);sync();window.dispatchEvent(new Event('activity-unlock'));}
 function begin(button){
  const k=key(button);lastKey=k;
  if(locks.has(k))return false;
  const record={armed:false};locks.set(k,record);setTimeout(()=>release(k,record),3000);
  // Defer native disabled until the button's default form submission has run.
  setTimeout(()=>{record.armed=true;sync();},0);return true;
 }
 document.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button||button.disabled||button.id==='clear-activity'||button.closest('#activity-search'))return;
  if(!begin(button)){event.preventDefault();event.stopImmediatePropagation();return;}
  log('Нажатие: '+button.textContent.trim()+(button.closest('[data-card-id]')?' · сигнал '+button.closest('[data-card-id]').dataset.cardId:''));
 },true);
 document.addEventListener('submit',event=>{
  const button=event.submitter||event.target.querySelector('button[type="submit"],button:not([type])');
  if(button){lastKey=key(button);if(!locks.has(lastKey))begin(button);}
 },true);
 new MutationObserver(sync).observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['disabled']});
 document.getElementById('clear-activity').onclick=()=>{lines.length=0;renderLog(true);};
 async function poll(id,watch){
  if(!reader||!watching.has(id))return;
  try{
   const data=await reader('/deliveries/'+encodeURIComponent(id));
   for(const client of data.recipients){
    const report=data.results[client];if(!report)continue;
    const signature=JSON.stringify([report.command_state,report.message]);
    if(watch.seen.get(client)!==signature){watch.seen.set(client,signature);log('Ответ · '+id+' · клиент '+client+' · '+(report.command_state||'получен')+(report.message?' · '+report.message:''));}
   }
   if(data.recipients.every(client=>data.results[client]?.command_state)){
    log('Все ответы получены · '+id+' · клиентов: '+data.recipients.length);
    release(watch.key,watch.lock);watching.delete(id);return;
   }
  }catch(error){if(!watch.error){log('Ожидание ответов · '+id+' · '+error.message);watch.error=true;}}
  if(Date.now()-watch.started>125000){log('Ожидание ответов завершено по тайм-ауту · '+id);watching.delete(id);return;}
  setTimeout(()=>poll(id,watch),Date.now()-watch.started<3000?300:2000);
 }
 return {log,sync,current:()=>({key:lastKey,lock:locks.get(lastKey)}),setReader:fn=>reader=fn,
  track(id,action){if(watching.has(id))return;const watch={...action,started:Date.now(),seen:new Map()};watching.set(id,watch);poll(id,watch);},
  stop(){watching.clear();locks.clear();sync();}
 };
})();
