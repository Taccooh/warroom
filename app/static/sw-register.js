if ('serviceWorker' in navigator) {
  // Register from the root — otherwise the scope is /static/ and the app under /
  // is not controlled (serviceWorker.ready hangs, push/offline dead).
  navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(function () {});
  // Clean up any old registration with the /static/ scope
  navigator.serviceWorker.getRegistrations().then(function (rs) {
    rs.forEach(function (r) { if (/\/static\/$/.test(r.scope)) r.unregister(); });
  }).catch(function () {});
}
