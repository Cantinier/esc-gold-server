'use strict';
const el=id=>document.getElementById(id);let password='',page=1,busy=false,total=0;
const number=v=>v===null||v===undefined?'—':new Intl.NumberFormat('ru-RU',{maximumFractionDigits:8}).format(v);
const date=v=>new Date(v*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'});
const reasons={0:'Вручную',1:'Мобильный терминал',2:'Веб-терминал',3:'Советник / API',4:'Stop Loss',5:'Take Profit',6:'Stop Out'};
function node(tag,text){const n=document.createElement(tag);n.textContent=text;return n;}
function fail(text=''){el('stats-error').textContent=text;el('stats-error').hidden=!text;}
async function load(){
 if(busy)return;busy=true;fail();
 for(const b of document.querySelectorAll('button'))b.disabled=true;
 try{
  const params=new URLSearchParams({search:el('stats-search').value.trim(),from:el('stats-from').value,to:el('stats-to').value,page:String(page)});
  const auth=btoa(String.fromCharCode(...new TextEncoder().encode('publisher:'+password)));
  const r=await fetch('/api/statistics?'+params,{headers:{Authorization:'Basic '+auth}});const data=await r.json();
  if(!r.ok)throw Error(r.status===401?'Неверный пароль отправителя.':data.error||'Ошибка сервера.');
  el('stats-login').hidden=true;el('stats-workspace').hidden=false;el('stats-logout').hidden=false;el('stats-password').value='';
  total=data.total;el('stats-count').textContent=`Найдено позиций: ${total}`;el('stats-page').textContent=`${page} / ${Math.max(1,Math.ceil(total/100))}`;
  el('stats-rows').replaceChildren();el('stats-totals').replaceChildren();el('stats-empty').hidden=total!==0;
  for(const t of data.totals){const card=node('div',t.currency+' · позиций: '+t.positions);card.className='card';const value=node('strong',number(t.net_profit));value.className=t.net_profit<0?'negative':'positive';card.append(value);el('stats-totals').append(card);}
  for(const r of data.rows){const tr=document.createElement('tr');
   const values=[date(r.closed_at),r.email,r.account,r.position_id,r.symbol,r.side,number(r.volume),number(r.entry_price),number(r.close_price),number(r.tp),number(r.sl),number(r.profit),number(r.commission),number(r.swap),number(r.fee),number(r.net_profit),r.currency,date(r.opened_at),reasons[r.reason]||String(r.reason)];
   values.forEach((v,i)=>{const td=node('td',v);if(i===2)td.append(node('small',r.broker+' · '+(r.trade_mode===2?'Реальный':'Демо / конкурс')));if(i===15)td.className=r.net_profit<0?'negative':'positive';tr.append(td);});el('stats-rows').append(tr);
  }
 }catch(e){fail(e.message||'Нет связи с сервером.');}
 finally{busy=false;for(const b of document.querySelectorAll('button'))b.disabled=false;el('stats-prev').disabled=page<=1;el('stats-next').disabled=page*100>=total;}
}
el('stats-login-form').onsubmit=e=>{e.preventDefault();password=el('stats-password').value;page=1;load();};
el('stats-filters').onsubmit=e=>{e.preventDefault();page=1;load();};
el('stats-refresh').onclick=()=>load();el('stats-prev').onclick=()=>{page--;load();};el('stats-next').onclick=()=>{page++;load();};
el('stats-reset').onclick=()=>{el('stats-filters').reset();page=1;load();};
el('stats-logout').onclick=()=>{password='';el('stats-workspace').hidden=true;el('stats-login').hidden=false;el('stats-logout').hidden=true;el('stats-rows').replaceChildren();el('stats-totals').replaceChildren();fail();};
