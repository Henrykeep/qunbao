const C="qunbao-v0.32.1";
self.addEventListener("install",e=>{self.skipWaiting()});
self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener("fetch",e=>{
  const r=e.request,u=new URL(r.url);
  if(r.method!=="GET"||u.origin!==location.origin)return;
  if(!(u.pathname==="/"||u.pathname==="/icon.png"||u.pathname==="/manifest.json"||u.pathname.startsWith("/api/state")||u.pathname.startsWith("/api/digest")||u.pathname.startsWith("/api/messages")))return;
  e.respondWith(fetch(r).then(res=>{if(res.ok&&res.status===200){const cp=res.clone();caches.open(C).then(c=>c.put(r,cp))}return res}).catch(()=>caches.match(r).then(m=>m||new Response("离线且无缓存",{status:503}))));
});
