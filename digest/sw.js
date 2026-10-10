const C="qunbao-v0.33.24";
self.addEventListener("install",e=>{self.skipWaiting()});
self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener("fetch",e=>{
  const r=e.request,u=new URL(r.url);
  if(r.method!=="GET"||u.origin!==location.origin)return;
  if(!(u.pathname==="/"||u.pathname==="/icon.png"||u.pathname==="/manifest.json"||u.pathname.startsWith("/api/state")||u.pathname.startsWith("/api/digest")||u.pathname.startsWith("/api/messages")))return;
  e.respondWith(fetch(r).then(res=>{if(res.ok&&res.status===200){const cp=res.clone();caches.open(C).then(c=>c.put(r,cp))}return res}).catch(()=>caches.match(r).then(m=>m||new Response("离线且无缓存",{status:503}))));
});
/* Web Push：服务器发来的通知。iOS 要求每条推送都必须弹出通知，不能静默 */
self.addEventListener("push",e=>{
  let d={};try{d=e.data?e.data.json():{}}catch(_){d={body:e.data?e.data.text():""}}
  const opt={body:d.body||"",icon:"/icon.png",badge:"/icon.png",data:{url:d.url||"/"}};
  if(d.tag){opt.tag=d.tag;opt.renotify=true}
  const jobs=[self.registration.showNotification(d.title||"群报",opt)];
  // 主屏幕图标角标 = 未完成待办数
  if(typeof d.badge==="number"&&self.navigator&&self.navigator.setAppBadge)
    jobs.push((d.badge>0?self.navigator.setAppBadge(d.badge):self.navigator.clearAppBadge()).catch(()=>{}));
  e.waitUntil(Promise.all(jobs));
});
/* 点通知：已经开着群报就切过去并定位到那件待办 / 那个群；没开就打开 */
self.addEventListener("notificationclick",e=>{
  e.notification.close();
  const url=new URL((e.notification.data&&e.notification.data.url)||"/",self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type:"window",includeUncontrolled:true}).then(ws=>{
    const w=ws.find(x=>new URL(x.url).origin===self.location.origin);
    if(w){w.postMessage({type:"qb-nav",url});return w.focus().catch(()=>{})}
    return self.clients.openWindow(url);
  }));
});
