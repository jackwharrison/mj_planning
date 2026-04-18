// Jack & Minke — Service Worker
// Handles background push notifications

self.addEventListener('push', function(event) {
  if (!event.data) return;

  let data;
  try {
    data = event.data.json();
  } catch(e) {
    data = { title: 'Jack & Minke', body: event.data.text() };
  }

  const options = {
    body:    data.body    || '',
    icon:    data.icon    || '/static/icon-192.png',
    badge:   data.badge   || '/static/icon-192.png',
    tag:     data.tag     || 'jm-notification',
    data:    { url: data.url || '/meals' },
    vibrate: [200, 100, 200],
    requireInteraction: false,
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'Jack & Minke', options)
  );
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/meals';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
      for (const client of list) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});