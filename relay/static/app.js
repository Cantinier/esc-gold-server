'use strict';
const $=id=>document.getElementById(id);let token='',checked=null,busy=false,locked=false;
const ids=['id','side','symbol','entry','end','targets','stop'];
function uuid(){const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const s=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');return `${s.slice(0,8)}-${s.slice(8,12)}-${s.slice(12,16)}-${s.slice(16,20)}-${s.slice(20)}`;}
function error(message=''){if(message)activity.log('Ошибка: '+message);$('error').hidden=!message;$('error').textContent=message;}
function save(){sessionStorage.setItem('sender-draft',JSON.stringify({values:Object.fromEntries(ids.map(id=>[id,$(id).value])),locked}));}
function price(v){v=v.trim().replace(',','.');if(!/^\d+(\.\d+)?$/.test(v)||!Number.isFinite(Number(v))||Number(v)<=0)throw Error('Введите положительные цены. Каждый тейк — с новой строки.');return v;}
function payload(){return {event_type:'open_order',event_body:{id:$('id').value,side:$('side').value,symbol:$('symbol').value,entry:$('end').value.trim()?[price($('entry').value),price($('end').value)]:price($('entry').value),take_profits:$('targets').value.trim().split(/\n|;/).map(price),stop_loss:price($('stop').value)}};}
function render(){try{$('json').textContent=JSON.stringify(payload(),null,2);}catch{$('json').textContent='Заполните вход, тейки и стоп — здесь появится JSON.';}}
function buttons(){$('check').disabled=busy;$('new').disabled=busy;$('send').disabled=busy||!checked;$('fields').disabled=busy||locked;}
async function rawApi(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{Authorization:'Basic '+btoa(String.fromCharCode(...new TextEncoder().encode('publisher:'+token))),...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});let data;try{data=await r.json();}catch{throw Error('Сервер вернул неверный ответ. Повторите с тем же ID.');}if(!r.ok){if(r.status===401)throw Error('Неверный пароль отправителя.');const messages={'No desktop clients connected; signal not accepted':'Нет подключённых десктопов. Подключите WebSocket в приложении.','Subscriber queue full; signal not accepted':'Очередь подписчика заполнена. Повторите позже с тем же ID.'};throw Error(messages[data.error]||data.error||(r.status===409?'Этот ID уже использован с другими параметрами.':'Ошибка сервера. Повторите с тем же ID.'));}return data;}
async function api(path,body){
 const action=activity.current();
 const label=body?.event_type?(commandNames[body.event_type]||body.event_type)+' · '+(body.event_body.signal_id||body.event_body.id)+(body.event_body.tp_index?' · TP '+body.event_body.tp_index:''):path;
 if(body)activity.log('Запрос: '+label);
 try{const result=await rawApi(path,body);
  if(body){activity.log('Принято: '+label+(result.queued_clients!==undefined?' · получателей: '+result.queued_clients:'')+(result.duplicate?' · повтор без рассылки':''));
   if(path==='/signals'&&result.id)activity.track(result.id,action);
  }
  return result;
 }catch(e){if(body)activity.log('Ошибка: '+label+' · '+e.message);throw e;}
}
activity.setReader(path=>rawApi(path));
window.addEventListener('activity-unlock',()=>{buttons();activity.sync();});
let previousConnectionState='';
async function status(){if(!token)return;try{const data=await api('/status');const state=`Десктопов онлайн: ${data.connected_clients}`;if(state!==previousConnectionState){activity.log(state);previousConnectionState=state;}$('status').textContent=state;await loadClients();await loadSignals();await loadNotifications();}catch{if(previousConnectionState!=='offline')activity.log('Нет связи с сервером');previousConnectionState='offline';$('status').textContent='Нет связи с сервером';}}
$('login-form').onsubmit=async e=>{e.preventDefault();token=$('token').value;error();try{await api('/status');$('login').hidden=true;$('workspace').hidden=false;$('activity-panel').hidden=false;activity.log('Авторизация выполнена');$('logout').hidden=false;$('publisher-settings').hidden=false;$('notifications-button').hidden=false;$('token').value='';await status();}catch(e){token='';error(e.message);}};
$('logout').onclick=()=>{activity.stop();$('activity-panel').hidden=true;closeClientDialog();$('online-clients').replaceChildren();$('all-clients').replaceChildren();token='';checked=null;$('login').hidden=false;$('workspace').hidden=true;$('logout').hidden=true;$('publisher-settings').hidden=true;$('notifications-button').hidden=true;$('notifications-dialog').close();$('notifications-list').replaceChildren();$('status').textContent='Вход не выполнен';buttons();};
for(const id of ids)$(id).addEventListener('input',()=>{checked=null;$('badge').textContent='Черновик';$('result').textContent='Сначала проверьте параметры.';save();render();buttons();});
$('signal-form').onsubmit=async e=>{e.preventDefault();if(busy)return;busy=true;checked=null;buttons();error();try{const data=await api('/validate',payload());checked=data;$('json').textContent=JSON.stringify(data,null,2);$('badge').textContent='Проверен';$('result').textContent=locked?'Параметры зафиксированы. Повторная отправка использует тот же ID.':'Проверка пройдена. Отправка автоматически запускает открытие ордеров в подключённых десктопах.';}catch(e){error(e.message);}finally{busy=false;buttons();}};
$('send').onclick=async()=>{if(busy||!checked)return;busy=true;locked=true;save();buttons();error();try{const r=await api('/signals',checked);$('result').textContent=r.duplicate?'Этот ID уже принят ранее. Повторная рассылка не выполнялась.':`Сигнал принят для рассылки. Подключённых десктопов: ${r.queued_clients}. Исполнение проверьте в MT5.`;$('badge').textContent='Принят';checked=null;}catch(e){error(e.message);$('result').textContent='Результат не подтверждён. Можно повторить отправку с тем же ID; новый сигнал не создаётся.';}finally{busy=false;buttons();await status();}};
$('new').onclick=()=>{if(busy)return;locked=false;checked=null;for(const id of ['entry','end','targets','stop'])$(id).value='';$('id').value=uuid();$('badge').textContent='Черновик';$('result').textContent='Сначала проверьте параметры.';error();save();render();buttons();};
try{const draft=JSON.parse(sessionStorage.getItem('sender-draft'));if(draft){for(const id of ids)if(typeof draft.values?.[id]==='string')$(id).value=draft.values[id];locked=!!draft.locked;}}catch{}
if(!$('id').value)$('id').value=uuid();save();render();buttons();setInterval(()=>{if(!document.hidden)status();},5000);

let clientActionBusy=false,clientSaving=false;
function node(tag,text,className=''){const el=document.createElement(tag);el.textContent=text;if(className)el.className=className;return el;}
let clientRows=[];const expandedClients=new Map();
function clientState(client){return client.revoked?'Ключ отозван':client.suspended?'Отключён администратором':client.connections?`Онлайн · соединений: ${client.connections}`:'Не подключён';}
function clientCard(client,online,query){
 const key=(online?'online':'all')+'|'+query+'|'+client.id;
 const card=node('article','','client-card compact-client');card.dataset.clientId=client.id;
 const heading=node('h3',client.description?.trim()||'Без описания');heading.title=client.description||'Без описания';card.append(heading);
 card.append(node('p',clientState(client),'client-state'));
 const toggle=node('button','','client-toggle');const detail=node('div','','client-detail');
 const dl=document.createElement('dl');
 for(const [label,value] of [['ID клиента',client.id],['Telegram',client.telegram],['Торговый счёт',client.trading_account],['Email',client.email],['Описание',client.description],['Устройство',client.device_fingerprint||'Ещё не привязано']])dl.append(node('dt',label),node('dd',value||'Не указано'));
 detail.append(dl);const actions=node('div','','client-actions');
 const add=(title,action,danger=false)=>{const button=node('button',title,danger?'danger':'');button.disabled=clientActionBusy;button.onclick=()=>controlClient(client,action);actions.append(button);};
 if(!client.revoked){if(online)add('Отключить','disconnect');if(!online&&client.suspended)add('Разрешить подключение','enable');add('Отозвать ключ','revoke',true);}add('Перевыпустить ключ','reissue');add('Сбросить устройство','reset-device',true);const edit=node('button','Редактировать описание');edit.onclick=()=>editDescription(client);actions.append(edit);detail.append(actions);
 let expanded=expandedClients.get(key)??!!query;
 const update=()=>{detail.hidden=!expanded;toggle.textContent=expanded?'Свернуть':'Развернуть';toggle.setAttribute('aria-expanded',String(expanded));};
 toggle.onclick=()=>{expanded=!expanded;expandedClients.set(key,expanded);update();};update();card.append(toggle,detail);return card;
}
function renderClients(){
 for(const online of [true,false]){
 const prefix=online?'online':'all',query=$(prefix+'-search').value.trim().toLocaleLowerCase('ru');
 const eligible=clientRows.filter(c=>online?c.connections>0:($('show-revoked').checked||!c.revoked));
 const rows=eligible.filter(c=>!query||[...Object.values(c),clientState(c)].join(' ').toLocaleLowerCase('ru').includes(query));
 $(online?'online-count':'client-count').textContent=query?`${rows.length} / ${eligible.length}`:rows.length;
 const list=$(prefix+'-clients');list.replaceChildren(...rows.map(c=>clientCard(c,online,query)));
 if(!rows.length)list.append(node('p',query?'Ничего не найдено.':online?'Нет подключённых клиентов.':'Нет клиентов для отображения.','muted'));
 }
}
async function loadClients(){if(!token)return;const data=await api('/clients');if(!token)return;clientRows=data.clients;renderClients();}
for(const prefix of ['online','all'])$(prefix+'-search').addEventListener('input',()=>{for(const key of expandedClients.keys())if(key.startsWith(prefix+'|')&&!key.startsWith(prefix+'||'))expandedClients.delete(key);renderClients();});
$('show-revoked').addEventListener('change',renderClients);
async function controlClient(client,action){if(clientActionBusy)return;clientActionBusy=true;error();try{const result=await api('/clients/'+encodeURIComponent(client.id)+'/'+action,{});if(action==='reissue'||action==='reset-device'){$('client-dialog-title').textContent='Новый ключ · '+(client.description||client.id);$('client-form').hidden=true;$('client-key-result').hidden=false;$('client-key').value=result.key;$('client-key-message').textContent=action==='reset-device'?'Привязка сброшена, старый ключ отключён. Передайте новый ключ нужному устройству: первое подключение закрепит новую привязку.':'Старый ключ отключён. Укажите новый ключ в десктопе и подключитесь заново.';$('client-error').textContent='';$('copy-client-key').textContent='Скопировать ключ';$('client-dialog').showModal();}await status();}catch(e){error(e.message);}finally{clientActionBusy=false;await loadClients().catch(()=>{});}}
function closeClientDialog(){if(clientSaving)return;$('client-dialog').close();$('client-key').value='';$('client-error').textContent='';}
$('new-client').onclick=()=>{$('client-dialog-title').textContent='Новый клиент';$('client-key-message').textContent='Клиент сохранён. Передайте ему этот ключ для подключения десктопа к WebSocket.';$('client-form').reset();$('client-id').value=uuid();$('client-form').hidden=false;$('client-key-result').hidden=true;$('client-key').value='';$('client-error').textContent='';$('copy-client-key').textContent='Скопировать ключ';$('client-dialog').showModal();};
$('close-client').onclick=closeClientDialog;
$('client-dialog').addEventListener('cancel',e=>{e.preventDefault();closeClientDialog();});
$('client-form').onsubmit=async e=>{e.preventDefault();if(clientSaving)return;clientSaving=true;$('logout').disabled=true;$('save-client').disabled=true;$('close-client').disabled=true;$('client-error').textContent='';try{const data=await api('/clients',{id:$('client-id').value.trim(),telegram:$('client-telegram').value.trim(),trading_account:$('client-account').value.trim(),email:$('client-email').value.trim(),description:$('client-description').value.trim()});$('client-form').hidden=true;$('client-key-result').hidden=false;$('client-key').value=data.key;await loadClients();}catch(e){$('client-error').textContent=e.message;}finally{clientSaving=false;$('logout').disabled=false;$('save-client').disabled=false;$('close-client').disabled=false;}};
$('copy-client-key').onclick=async()=>{const field=$('client-key');field.focus();field.select();try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(field.value);else if(!document.execCommand('copy'))throw Error();$('copy-client-key').textContent='Скопировано';}catch{$('client-error').textContent='Выделенный ключ можно скопировать вручную.';}};

const commandNames={terminate_signal:'Закрыть весь сигнал',terminate_tp:'Отменить / закрыть TP',modify_tp:'Изменить ТП',breakeven_signal:'Стоп в БУ',breakeven_tp:'Стоп в БУ по TP'};
const legStates={pending:'отложенный ордер',position:'позиция',partial_fill:'частичное исполнение',closed:'закрыт',not_opened:'не открыт',unknown:'состояние не подтверждено'};
const commandStates={complete:'выполнено',partial:'выполнено частично',error:'ошибка',ignored:'пропущено — нет сигнала'};
let commandBusy=false,tpContext=null,pendingCommands={};
try{pendingCommands=JSON.parse(sessionStorage.getItem('signal-commands')||'{}');}catch{}
async function sendCommand(signal,kind,extra={}){
 if(commandBusy)return false;commandBusy=true;error();
 const result=$('command-result');result.hidden=false;result.textContent='Отправка команды…';
 try{
 const key=JSON.stringify([signal.id,kind,extra]);
 const packet=pendingCommands[key]||{event_type:kind,event_body:{id:uuid(),signal_id:signal.id,...extra}};
 pendingCommands[key]=packet;sessionStorage.setItem('signal-commands',JSON.stringify(pendingCommands));
 const r=await api('/signals',packet);delete pendingCommands[key];sessionStorage.setItem('signal-commands',JSON.stringify(pendingCommands));
 result.textContent=r.duplicate?'Команда уже принята ранее; повторной рассылки нет.':r.queued_clients===0?'Команда принята · получателей: 0. В MT5 ничего не изменено: нет подключённых клиентов.':`Команда отправлена ${r.queued_clients} клиентам. Ожидаем подтверждение MT5.`;
 await loadSignals();return true;
 }catch(e){result.textContent='Результат команды не подтверждён.';error(e.message+' Повтор этого же действия использует тот же ID команды.');return false;}finally{commandBusy=false;}
}
let historyDeleting=false;
async function deleteHistory(signal=null){
 if(historyDeleting)return;
 historyDeleting=true;$('delete-history').disabled=true;error();
 try{const result=await api('/signals/history/delete',signal?{id:signal.id}:{all:true});
 const message=$('command-result');message.hidden=false;message.textContent=`Удалено из истории: ${result.deleted}.`;
 if(signal){signalCardsOpen.delete(signal.id);signalDetailsOpen.delete(signal.id);}
 await loadSignals();
 }catch(e){error(e.message);}finally{historyDeleting=false;$('delete-history').disabled=false;}
}
$('delete-history').onclick=()=>deleteHistory();
const signalDetailsOpen=new Map();
const signalCardsOpen=new Map();
function signalLegClosed(signal,index){
 const clients=[...new Set([...signal.recipients,...Object.keys(signal.reports)])];
 return clients.length>0 && clients.every(client=>{
  const leg=signal.reports[client]?.legs?.find(l=>l.tp_index===index);
  return leg && ['closed','not_opened'].includes(leg.state);
 });
}
function signalCard(signal){
 const body=signal.payload.event_body,card=node('details','','signal-card'),content=node('div','','signal-content');
 card.dataset.cardId=signal.id;card.open=signalCardsOpen.get(signal.id)||false;
 const entry=Array.isArray(body.entry)?body.entry.join(' — '):body.entry;
 const created=new Date(signal.created*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
 const heading=node('summary',`${body.side} · ${body.symbol} | ${entry} / ${created}`,'signal-heading');
 heading.title='Время создания: МСК. Нажмите, чтобы развернуть или свернуть сигнал.';
 card.append(heading,content);
 if(signal.archived){const remove=node('button','Удалить','danger');remove.onclick=()=>deleteHistory(signal);const actions=node('div','','client-actions');actions.append(remove);content.append(actions);}
 if(signal.archived)content.append(node('p','В истории · исполнение команд смотрите в ответах клиентов.','muted'));
 if(!signal.recipients.length)content.append(node('p','Получателей при публикации: 0.','muted'));
 content.append(node('p',signal.id,'signal-id'),node('p','Вход: '+(Array.isArray(body.entry)?body.entry.join(' — '):body.entry)+' · SL: '+body.stop_loss));
 const add=(parent,title,kind,extra={})=>{const b=node('button',title,kind.startsWith('terminate')?'danger':'');b.dataset.actionKey=signal.id+'|'+kind+'|'+(extra.tp_index||'');b.onclick=()=>sendCommand(signal,kind,extra);parent.append(b);};
 {const actions=node('div','','client-actions');add(actions,'Закрыть весь сигнал','terminate_signal');add(actions,'Стоп в БУ','breakeven_signal');content.append(actions);}
 body.take_profits.forEach((target,i)=>{const leg=node('div','','signal-leg');leg.append(node('p',`TP ${i+1} · исходная цена ${target}`));
 for(const client of signal.recipients){const report=signal.reports[client];const actual=report?.legs?.find(l=>l.tp_index===i+1);leg.append(node('p',client+': '+(actual?(legStates[actual.state]||actual.state)+' · TP '+actual.tp:'ожидание подтверждения'),'muted'));}
 if(!signalLegClosed(signal,i+1)){const actions=node('div','','client-actions');add(actions,'Отменить / закрыть','terminate_tp',{tp_index:i+1});add(actions,'Стоп в БУ','breakeven_tp',{tp_index:i+1});const edit=node('button','Изменить ТП');edit.dataset.actionKey=signal.id+'|modify_tp|'+(i+1);edit.onclick=()=>{tpContext={signal,index:i+1};$('tp-label').textContent=signal.id+' · TP '+(i+1);const reports=Object.values(signal.reports).flatMap(r=>r.legs||[]);$('tp-price').value=reports.find(l=>l.tp_index===i+1)?.tp||target;$('tp-error').textContent='';$('tp-dialog').showModal();};actions.append(edit);leg.append(actions);}content.append(leg);});
 const details=node('details','');details.dataset.signalId=signal.id;details.open=signalDetailsOpen.get(signal.id)||false;details.append(node('summary','Ответы клиентов и команды'));
 for(const client of signal.recipients){const r=signal.reports[client];details.append(node('p',client+': '+(r?.state==='closed'?'закрыт':r?.state==='active'?'активен':'ожидание отчёта')+(r?.message?' · '+r.message:'')));}
 for(const command of signal.commands){const p=node('p',(commandNames[command.kind]||command.kind)+' · '+new Date(command.created*1000).toLocaleString(),'command-status');const missing=signal.recipients.filter(c=>!command.recipients.includes(c));if(missing.length)p.append(node('br',''),document.createTextNode('Не получили команду: '+missing.join(', ')));if(!command.recipients.length)p.append(node('br',''),document.createTextNode('Принято · получателей: 0'));for(const client of command.recipients){const r=command.results[client];p.append(node('br',''),document.createTextNode(client+': '+(commandStates[r?.command_state]||'ожидание выполнения')+(r?.message?' · '+r.message:'')));}details.append(p);}content.append(details);return card;
}
async function loadSignals(){if(!token)return;const data=await api('/signals');if(!token)return;for(const card of document.querySelectorAll('.signal-card[data-card-id]'))signalCardsOpen.set(card.dataset.cardId,card.open);for(const details of document.querySelectorAll('.signal-card details[data-signal-id]'))signalDetailsOpen.set(details.dataset.signalId,details.open);for(const [prefix,archived] of [['active',false],['history',true]]){const rows=data.signals.filter(s=>!!s.archived===archived);$(prefix+'-count').textContent=rows.length;$(prefix+'-signals').replaceChildren(...rows.map(signalCard));if(!rows.length)$(prefix+'-signals').append(node('p',archived?'История пока пуста.':'Опубликованные сигналы появятся здесь.','muted'));}}
$('close-tp').onclick=()=>{$('tp-dialog').close();};
$('tp-form').onsubmit=async e=>{e.preventDefault();if(commandBusy)return;try{const new_tp=price($('tp-price').value);const ok=await sendCommand(tpContext.signal,'modify_tp',{tp_index:tpContext.index,new_tp});if(ok)$('tp-dialog').close();else $('tp-error').textContent='Команда не подтверждена. Сообщение об ошибке показано в панели.';}catch(e){$('tp-error').textContent=e.message;}};

let descriptionClient=null,descriptionSaving=false;
function editDescription(client){descriptionClient=client.id;$('description-client').textContent=client.description||'Без описания';$('description-value').value=client.description||'';$('description-error').textContent='';$('description-dialog').showModal();$('description-value').focus();}
function closeDescription(){if(!descriptionSaving)$('description-dialog').close();}
$('close-description').onclick=closeDescription;
$('description-dialog').addEventListener('cancel',e=>{e.preventDefault();closeDescription();});
$('description-form').onsubmit=async e=>{e.preventDefault();if(descriptionSaving)return;descriptionSaving=true;$('save-description').disabled=true;$('close-description').disabled=true;$('description-error').textContent='';try{const result=await api('/clients/'+encodeURIComponent(descriptionClient)+'/description',{description:$('description-value').value.trim()});clientRows=clientRows.map(c=>c.id===result.id?{...c,description:result.description}:c);renderClients();$('description-dialog').close();}catch(e){$('description-error').textContent=e.message;}finally{descriptionSaving=false;$('save-description').disabled=false;$('close-description').disabled=false;}};

let publisherSaving=false;
$('publisher-settings').onclick=()=>{
 $('publisher-password').value='';$('publisher-repeat').value='';$('publisher-result').textContent='';
 $('apply-publisher').disabled=false;$('publisher-dialog').showModal();$('publisher-password').focus();
};
function closePublisher(){if(!publisherSaving){$('publisher-dialog').close();$('publisher-password').value='';$('publisher-repeat').value='';}}
$('close-publisher').onclick=closePublisher;
$('publisher-dialog').addEventListener('cancel',e=>{e.preventDefault();closePublisher();});
$('publisher-form').onsubmit=async event=>{
 event.preventDefault();if(publisherSaving)return;
 const next=$('publisher-password').value,previous=token;
 if(next!==$('publisher-repeat').value){$('publisher-result').textContent='Пароли не совпадают.';return;}
 publisherSaving=true;$('apply-publisher').disabled=true;$('close-publisher').disabled=true;
 try{
  try{await api('/publisher/password',{new_password:next});}
  catch(error){token=next;try{await rawApi('/status');}catch{token=previous;throw error;}}
  token=next;activity.log('Пароль отправителя изменён. Старый пароль больше не действует.');
  $('publisher-result').textContent='Пароль изменён. Эта вкладка уже использует новый пароль.';
  $('publisher-password').value='';$('publisher-repeat').value='';
 }catch(error){$('publisher-result').textContent=error.message+' Если ответ потерян, проверьте вход с новым паролем.';$('apply-publisher').disabled=false;}
 finally{publisherSaving=false;$('close-publisher').disabled=false;}
};

let noticeRows=[];
async function loadNotifications(){
 if(!token)return;const data=await api('/notifications');if(!token)return;
 noticeRows=data.notifications;$('notifications-count').textContent=data.unread;
 $('notifications-button').classList.toggle('has-alerts',data.unread>0);
 if($('notifications-dialog').open)renderNotifications();
}
function renderNotifications(){
 const list=$('notifications-list');list.replaceChildren();
 for(const notice of noticeRows){
  const item=node('article','','client-card');
  item.append(node('h3',notice.description||notice.client_id),node('p',notice.message),
   node('p',new Date(notice.created*1000).toLocaleString('ru-RU')+' · IP: '+notice.ip,'muted'),
   node('p','Клиент: '+notice.client_id+(notice.email?' · '+notice.email:''),'muted'));
  list.append(item);
 }
 if(!noticeRows.length)list.append(node('p','Уведомлений пока нет.','muted'));
}
$('notifications-button').onclick=async()=>{
 $('notifications-dialog').showModal();
 try{await loadNotifications();renderNotifications();if(noticeRows.length){await api('/notifications/read',{through_id:noticeRows[0].id});await loadNotifications();}}
 catch(e){error(e.message);}
};
$('close-notifications').onclick=()=>$('notifications-dialog').close();
