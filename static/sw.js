// Service Worker：Web Push 系统通知 + 基础离线缓存
// 换图标/换样式后记得把 CACHE 版本号 +1，否则旧缓存会继续被用
const CACHE = 'fund-monitor-v2';
const ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.json',
  // 预缓存通知图标：离线收到推送时也有图标可显示
  '/static/icons/icon-192.png?v=2',
  '/static/icons/apple-touch-icon.png?v=2',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  // 逐个缓存 + 各自 catch：addAll 是全有全无，任何一个 404 会导致整个预缓存失败
  e.waitUntil(
    caches
      .open(CACHE)
      .then((c) => Promise.all(ASSETS.map((u) => c.add(u).catch(() => {}))))
      .catch(() => {})
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => clients.claim())
  );
});

// 网络优先，失败回退缓存（基金数据要新，但离线也能看上次数据）
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || new Response('', { status: 504 })))
  );
});

// 接收 push，显示系统通知（即使浏览器/PWA 不在前台）
self.addEventListener('push', (e) => {
  let data = { title: '基金涨跌监控', body: '有新提醒' };
  try {
    data = e.data.json();
  } catch (err) {
    if (e.data) data.body = e.data.text();
  }
  e.waitUntil(
    self.registration.showNotification(data.title || '基金涨跌监控', {
      body: data.body || '',
      icon: '/static/icons/icon-192.png?v=2',
      badge: '/static/icons/icon-192.png?v=2',
      tag: 'fund-alert',
      renotify: true,
      vibrate: [80, 40, 80],
      data: { url: '/' },
    })
  );
});

// 点击通知：聚焦已有窗口或打开新窗口
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if (c.url.includes(self.location.origin) && 'focus' in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow('/');
    })
  );
});
