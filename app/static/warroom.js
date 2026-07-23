document.addEventListener('DOMContentLoaded', function () {
  // Confirm-before-submit forms carry a data-confirm message instead of an
  // inline onsubmit="" — CSP script-src has no 'unsafe-inline'.
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) {
      if (!confirm(f.dataset.confirm)) e.preventDefault();
    });
  });
  var watchSelect = document.getElementById('watch-level-select');
  if (watchSelect) watchSelect.addEventListener('change', function () { this.form.submit(); });

  // Per-request data, handed over as a JSON island (not executable — exempt
  // from script-src) so this file can stay static instead of Jinja-templated.
  var DATA = JSON.parse(document.getElementById('warroom-data').textContent);
  var cells = DATA.cells;
  var targets = DATA.targets;
  var theatres = DATA.theatres;
  var grid = DATA.grid;
  // Virgin land arrives as a flat index list [i,j,i,j,…] — we compute lat/lng here
  // (with thousands of cells that saves ~80% payload). Needs grid → only here.
  function expandVirgin(flat) {
    var out = [];
    for (var k = 0; k + 1 < flat.length; k += 2) {
      var i = flat[k], j = flat[k + 1];
      out.push({i: i, j: j, lat: (i + 0.5) * grid.lat, lng: (j + 0.5) * grid.lng});
    }
    return out;
  }
  var virginCells = expandVirgin(DATA.virginAll);
  var T = DATA.js;
  var SHARING = DATA.sharing;
  var HISTORY = DATA.history;
  var initTab = DATA.initTab;
  var POLL_EPOCH = DATA.pollEpoch;
  // Data-freshness clock (step 7): reset whenever fresh poll data lands (page render
  // or an applyLive). The here-banner shows "live" / "Nm" from it — a glanceable
  // "is my data still updating?" that goes stale if the poller ever stalls.
  var dataAt = Date.now();
  var COLOR = {mine: '#e8b64c', enemy: '#c9313d', free: '#5f7789'};

  function tf(s, o) { return s.replace(/\{(\w+)\}/g, function (_, k) { return o[k]; }); }
  function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

  var cellByKey = {};
  cells.forEach(function (c) { cellByKey[c.i + '_' + c.j] = c; });
  function cellAt(lat, lng) {
    var i = Math.floor(lat / grid.lat + 1e-9), j = Math.floor(lng / grid.lng + 1e-9);
    return cellByKey[i + '_' + j] || null;
  }

  // Attribution lives behind the ⓘ button (bottom right) instead of the permanent
  // banner — see the InfoCtl control below. Links stay one tap away (OSM requires
  // accessible attribution), they just don't cover the map corner all the time.
  var map = L.map('map', {zoomControl: true, zoomSnap: 0.5, attributionControl: false}).setView([50, -20], 3);
  // Map controls live in a right-edge column (UI redesign step 5): thumb reach, and
  // Leaflet's bottom-right corner sits at the map's bottom edge = right above the
  // sheet, so the buttons stay reachable as the sheet is dragged.
  map.zoomControl.setPosition('bottomright');
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18
  }).addTo(map);

  // ⓘ control: collapsed map credits. Tap toggles the box; tapping the map closes it.
  var InfoCtl = L.Control.extend({options: {position: 'bottomright'}, onAdd: function () {
    var d = L.DomUtil.create('div', 'leaflet-bar info-ctl');
    d.innerHTML = '<div id="attrib-box" class="attrib-box" hidden>' +
      '<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a> · &copy; ' +
      '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors' +
      '</div><a href="#" id="attrib-btn" role="button" title="Info">&#9432;</a>';
    L.DomEvent.disableClickPropagation(d);
    return d;
  }});
  map.addControl(new InfoCtl());
  document.getElementById('attrib-btn').addEventListener('click', function (e) {
    e.preventDefault();
    var b = document.getElementById('attrib-box');
    b.hidden = !b.hidden;
  });
  map.on('click', function () {
    var b = document.getElementById('attrib-box');
    if (b && !b.hidden) b.hidden = true;
  });

  // ---- Units: metric vs imperial ----
  // One switch drives every distance the client renders (planner rows, ring
  // labels/steps, tour total, nav guidance). Auto-default: en-US browsers get
  // miles (Canada thinks in km, so language alone would be wrong); manual
  // override per device via the Info-tab toggle, stored in localStorage.
  var MI_KM = 1.609344;
  var units = null;
  try { units = localStorage.getItem('wr_units'); } catch (e) {}
  if (units !== 'mi' && units !== 'km') {
    units = ((navigator.language || '').toLowerCase() === 'en-us') ? 'mi' : 'km';
  }
  function fmtDist(km) {   // km (float) → "3.4 km" / "2.1 mi"
    var v = units === 'mi' ? km / MI_KM : km;
    return (v < 10 ? Math.round(v * 10) / 10 : Math.round(v)) + ' ' + units;
  }
  var unitsBtn = document.getElementById('units-toggle');
  function unitsBtnText() {
    if (unitsBtn) unitsBtn.textContent = units === 'mi' ? 'Imperial (mi)' : 'Metric (km)';
  }
  unitsBtnText();
  if (unitsBtn) unitsBtn.addEventListener('click', function () {
    units = units === 'mi' ? 'km' : 'mi';
    try { localStorage.setItem('wr_units', units); } catch (e) {}
    unitsBtnText();
    plRender();        // planner distances
    renderRings();     // ring labels + step series
    renderTour();      // tour total line
    var mp = myPos(); if (mp) navUpdate(mp.lat, mp.lng);   // nav banner distance
  });

  var rects = [];
  var cellLayer = L.layerGroup().addTo(map);
  function renderCells() {
    cellLayer.clearLayers();
    rects = [];
    cellByKey = {};
    cells.forEach(function (c) { cellByKey[c.i + '_' + c.j] = c; });
    cells.forEach(function (c) {
      var lead = c.status === 'enemy' && c.gap === 0;
      // Enemies in their real gang color (CHAOS vs BWM distinguishable), own gold, free cold
      var fill = c.status === 'enemy' ? (c.color || COLOR.enemy) : COLOR[c.status];
      var r = L.rectangle(c.b, {
        color: lead ? '#ffd15e' : fill, weight: lead ? 2 : 1,
        opacity: c.status === 'free' ? 0.6 : 0.9, fillColor: fill,
        fillOpacity: c.status === 'free' ? 0.12 : 0.4, dashArray: c.status === 'free' ? '3' : null
      }).addTo(cellLayer);
      var label = c.status === 'mine' ? T.gang_your
        : c.status === 'enemy' ? (c.count == null ? tf(T.gang_here, {g: esc(c.gang)})
                                                   : tf(T.gang_holds, {g: esc(c.gang), n: c.count}))
        : T.unclaimed;
      var extra = c.status === 'enemy'
        ? '<br>' + (c.gap == null ? T.fog_strength
                    : c.gap === 0 ? '<b>' + T.lead_excl + '</b>'
                    : tf(T.to_flip, {n: c.gap}))
        : '';
      // GSM masts count as ordinary scans — surface the tally as extra info
      if (c.towers) extra += '<br>' + (c.towers === 1 ? T.masts_pop_one : tf(T.masts_pop, {n: c.towers}));
      var cc = [(c.i + 0.5) * grid.lat, (c.j + 0.5) * grid.lng];
      r.bindPopup('<b>' + label + '</b><br>' + tf(T.your_aps, {n: (c.my_aps || 0)}) + extra +
        '<br><button type="button" class="cell-tour" data-lat="' + cc[0].toFixed(5) + '" data-lng="' + cc[1].toFixed(5) +
        '" data-label="' + label.replace(/<[^>]*>/g, '').replace(/"/g, '&quot;') + '">' + T.tour_add_pin + '</button>');
      rects.push(r);
    });
  }
  renderCells();

  // ---- Virgin land: never-scanned cells. Separate layer, toggled via chip —
  // there are quickly thousands of them, they must not plaster the map permanently.
  var virginLayer = L.layerGroup();
  var virginOn = false;
  var virginByKey = {};
  function indexVirgin() {
    virginByKey = {};
    virginCells.forEach(function (v) { virginByKey[v.i + '_' + v.j] = v; });
  }
  indexVirgin();
  function renderVirgin() {
    virginLayer.clearLayers();
    if (!virginOn) return;
    virginCells.forEach(function (v) {
      var b = [[v.i * grid.lat, v.j * grid.lng],
               [(v.i + 1) * grid.lat, (v.j + 1) * grid.lng]];
      L.rectangle(b, {color: '#8fa7bd', weight: 1, opacity: 0.5, fillColor: '#8fa7bd',
                      fillOpacity: 0.10, dashArray: '2 3'})
        .bindPopup('<b>' + T.virgin_land + '</b><br>' + T.virgin_pop +
          '<br><button type="button" class="cell-tour" data-lat="' + v.lat.toFixed(5) +
          '" data-lng="' + v.lng.toFixed(5) + '" data-label="' + T.virgin_land +
          '">' + T.tour_add_pin + '</button>')
        .addTo(virginLayer);
    });
  }
  // Toggle called from the ◈ layers popover (built further below). The popover row
  // reflects state via reflectLayers(); no rail chip owns this anymore.
  function toggleVirgin() {
    virginOn = !virginOn;
    if (virginOn) { virginLayer.addTo(map); renderVirgin(); snapVirginWater(); }
    else { virginLayer.clearLayers(); map.removeLayer(virginLayer); }
    if (typeof reflectLayers === 'function') reflectLayers();
  }

  // ---- GSM masts: informational overlay of towers logged per cell. wdgwars first
  // shipped a mast-ownership layer and reverted it two days later — masts now count
  // as ordinary scans, so this is purely a "where are the towers" view. Own layer,
  // toggled from the ◈ popover, violet shaded by tower density (more masts = denser).
  var mastsLayer = L.layerGroup();
  var mastsOn = false;
  function renderMasts() {
    mastsLayer.clearLayers();
    if (!mastsOn) return;
    cells.forEach(function (c) {
      if (!c.towers) return;
      var b = [[c.i * grid.lat, c.j * grid.lng],
               [(c.i + 1) * grid.lat, (c.j + 1) * grid.lng]];
      // fill scales with the count (1 mast ≈ 0.12, saturates ~12 masts at 0.42)
      var op = 0.10 + Math.min(c.towers, 12) * 0.0266;
      L.rectangle(b, {color: '#c04bff', weight: 1, opacity: 0.85, fillColor: '#c04bff',
                      fillOpacity: op, className: 'masts-cell'})
        .bindPopup('<b>' + (c.towers === 1 ? T.masts_pop_one : tf(T.masts_pop, {n: c.towers})) + '</b>')
        .addTo(mastsLayer);
    });
  }
  function toggleMasts() {
    mastsOn = !mastsOn;
    if (mastsOn) { mastsLayer.addTo(map); renderMasts(); }
    else { mastsLayer.clearLayers(); map.removeLayer(mastsLayer); }
    if (typeof reflectLayers === 'function') reflectLayers();
  }
  // Virgin cells sit purely on geometry, so some land in lakes/rivers (Lake Erie).
  // Snap the nearest ones to a road via /api/snap; cells with no drivable road
  // (water/forest) come back null → drop them from virginCells. This runs once on load
  // (not only when the layer is toggled) so the tour/target list — which reads
  // virginCells live — never offers a water cell as the nearest target. Bounded to the
  // nearest ~120 to keep Overpass light; results cache server-side so they stay hidden.
  var virginSnapped = false;
  function snapVirginWater() {
    if (virginSnapped || !virginCells.length) return;
    virginSnapped = true;
    var near = virginCells.slice(0, 120), batches = [];
    for (var i = 0; i < near.length; i += 40) batches.push(near.slice(i, i + 40));
    var water = {};
    (function run(bi) {
      if (bi >= batches.length) return;
      fetch('/api/snap', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cells: batches[bi].map(function (v) { return [v.i, v.j]; })})})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (d && d.points) {
            Object.keys(d.points).forEach(function (k) { if (d.points[k] === null) water[k] = 1; });
            if (Object.keys(water).length) {
              virginCells = virginCells.filter(function (v) { return !water[v.i + '_' + v.j]; });
              indexVirgin(); if (virginOn) renderVirgin();
            }
          }
          run(bi + 1);
        })
        .catch(function () { run(bi + 1); });
    })(0);
  }

  function fitAll() {
    if (rects.length) map.fitBounds(L.featureGroup(rects).getBounds(), {padding: [30, 30]});
  }
  function fitInitial() {
    if (theatres.length) map.fitBounds(theatres[0].bounds, {padding: [30, 30]});
    else fitAll();
  }
  fitInitial();
  // Filter water cells out of the target/tour list even without a layer toggle.
  snapVirginWater();

  document.querySelectorAll('.rb-jump[data-b]').forEach(function (b) {
    b.addEventListener('click', function () { map.fitBounds(JSON.parse(b.dataset.b), {padding: [30, 30]}); });
  });
  var ja = document.getElementById('jump-all');
  if (ja) ja.addEventListener('click', fitAll);

  // Delegation instead of individual handlers: the planner list is replaced by the live update
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    if (e.target.closest('.tour-add')) return;  // the + button must not fly along
    if (e.target.closest('.pl-chip') || e.target.closest('.pl-sort')) return;
    var el = e.target.closest('.pl-item[data-lat]');
    if (el) map.flyTo([parseFloat(el.dataset.lat), parseFloat(el.dataset.lng)], 12, {duration: 0.7});
  });

  // ---- Planner: filter + sorting + distance ----
  // Separate steps because they differ in cost: filtering only toggles
  // `hidden` (cheap), sorting rebuilds the list (expensive → only when needed, and in
  // ONE go via a fragment instead of 180 individual moves).
  var plFilter = {mode: 'all', gang: null};
  var plSort = 'dist';
  var plLastSortAt = 0;

  // Window size: this many rows live in the DOM. The rest stays in memory —
  // a large turf can have thousands of cells, which would strangle the phone.
  var PL_PAGE = 60;
  var plShown = PL_PAGE;
  var plRef = null;      // reference point of the most recently computed distances

  function plRef_() {
    return myPos() || (map ? {lat: map.getCenter().lat, lng: map.getCenter().lng} : null);
  }

  // All candidates as data: enemy + free (from the server) and virgin land (all cells).
  function plCandidates() {
    var out = targets.slice();
    virginCells.forEach(function (v) {
      out.push({t: 'virgin', lat: v.lat, lng: v.lng, my: 0, gap: 0});
    });
    return out;
  }

  function plMatch(c) {
    var m = plFilter.mode;
    return m === 'all'
      || (m === 'ahead' && c.t === 'enemy' && c.gap === 0)
      || (m === 'free' && c.t === 'free')
      || (m === 'virgin' && c.t === 'virgin')
      || (m === 'gang' && c.t === 'enemy' && c.g === plFilter.gang);
  }

  function plEffort(c) {
    // Fogged enemy cells (gap unknown) sort last under "easiest first" — we can't
    // rank an unknown deficit, so don't pretend it's 0.
    if (c.t === 'free') return 0;
    if (c.t === 'virgin') return 1;
    return c.gap == null ? 9e9 : c.gap + 1;
  }

  function plRow(c, gps) {
    var li = document.createElement('li');
    li.className = 'pl-item' + (c.t === 'enemy' && c.gap === 0 ? ' pl-lead' : '');
    li.dataset.lat = c.lat;
    li.dataset.lng = c.lng;
    var dist = (gps && c._d != null) ? fmtDist(c._d) : '';
    var label, dot, tag, line;
    if (c.t === 'enemy') {
      label = esc(c.g);
      dot = '<span class="pl-dot"' + (c.c ? ' style="background:' + esc(c.c) + '"' : '') + '></span>';
      if (c.gap == null) {   // feed fogs enemy strength this season — no bogus gap/bar
        tag = '<span class="pl-gap fog">' + esc(T.fog_tag) + '</span>';
        line = '<div class="pl-row2">' + esc(T.fog_line) + '</div>';
      } else {
        // Row diet: the bar already encodes my-vs-their — no text repeat of it.
        tag = '<span class="pl-gap' + (c.gap === 0 ? ' lead' : '') + '">' +
              (c.gap === 0 ? esc(T.lead) : esc(tf(T.gap_aps, {n: c.gap}))) + '</span>';
        var pct = c.gap === 0 ? 100 : Math.round(100 * c.my / (c.cnt + 1));
        line = '<div class="pl-bar"><i style="width:' + pct + '%"></i></div>';
      }
    } else if (c.t === 'free') {
      label = '<span class="free-name">' + esc(T.free_tag) + '</span>';
      dot = '<span class="pl-dot free"></span>';
      tag = '<span class="pl-gap free">' + esc(T.free_grab) + '</span>';
      // second line only when it carries information (own APs already there)
      line = c.my ? '<div class="pl-row2">' + tf(T.free_line, {my: c.my}) + '</div>' : '';
    } else {
      // "Virgin land" + "untouched" tag say it all — the explainer row repeated it
      label = '<span class="virgin-name">' + esc(T.virgin_land) + '</span>';
      dot = '<span class="pl-dot virgin"></span>';
      tag = '<span class="pl-gap virgin">' + esc(T.virgin_tag) + '</span>';
      line = '';
    }
    li.innerHTML =
      '<div class="pl-main"><div class="pl-row1">' + dot +
        '<span class="pl-gang">' + label + '</span>' +
        '<span class="pl-dist">' + dist + '</span>' + tag +
      '</div>' + line + '</div>' +
      '<button type="button" class="tour-add" data-lat="' + c.lat + '" data-lng="' + c.lng +
        '" data-label="' + esc(c.t === 'enemy' ? c.g : c.t === 'free' ? T.free_tag : T.virgin_land) +
        '">+</button>';
    return li;
  }

  // Filtering + sorting run over ALL candidates (otherwise "nearest first" would
  // again just be sorting a pre-selection) — only the window is rendered.
  function plRender() {
    var list = document.getElementById('pl-list');
    if (!list) return;
    var gps = myPos();
    var ref = plRef_();
    plRef = ref;

    var cand = plCandidates().filter(plMatch);
    if (ref) {
      cand.forEach(function (c) { c._d = hav(ref, {lat: c.lat, lng: c.lng}); });
    }
    var key = plSort === 'dist' ? function (c) { return c._d != null ? c._d : 1e9; }
            : plSort === 'aps' ? function (c) { return -(c.my || 0); }
            // "Worth the drive": effort × air-distance, ascending — the score is a
            // ranking proxy (straight-line, not road km) and stays internal; rows
            // keep showing only the honest gap + distance. Fogged cells sort last
            // (unknown effort never pretends to be cheap); without a position the
            // distance factor collapses to 1 → pure effort order.
            : plSort === 'worth' ? function (c) {
                var e = plEffort(c);
                return e >= 9e9 ? 9e18 : (e + 1) * (c._d != null ? c._d : 1);
              }
            : plEffort;
    cand.sort(function (a, b) { return key(a) - key(b); });

    var page = cand.slice(0, plShown);
    var frag = document.createDocumentFragment();
    page.forEach(function (c) { frag.appendChild(plRow(c, gps)); });
    list.innerHTML = '';
    list.appendChild(frag);

    var none = document.getElementById('pl-none');
    if (none) none.hidden = cand.length > 0;
    var more = document.getElementById('pl-more');
    if (more) {
      more.hidden = cand.length <= page.length;
      var txt = document.getElementById('pl-more-txt');
      if (txt) txt.textContent = tf(T.shown_of, {k: page.length, n: cand.length});
    }
    var chip = document.querySelector('.pl-chip.on b');
    if (chip && plFilter.mode !== 'gang') chip.textContent = cand.length;
    plLastSortAt = Date.now();
    renderTour();   // set the "+" states of the fresh rows
  }

  function plRefresh() { plShown = PL_PAGE; plRender(); }

  // While driving, GPS fires every second. Rebuild only when the reference
  // point has moved noticeably (>300 m) or 10 s have passed — otherwise the list
  // jumps away from under your thumb. The first fix takes effect immediately.
  var plHadGps = false;
  function plOnPosition() {
    var ref = plRef_();
    if (!ref) return;
    if (!plHadGps) { plHadGps = true; plRender(); return; }
    if (plSort !== 'dist') return;
    var moved = plRef ? hav(plRef, ref) : 999;
    if (moved > 0.3 && Date.now() - plLastSortAt > 10000) plRender();
  }

  document.addEventListener('click', function (e) {
    var chip = e.target.closest ? e.target.closest('.pl-chip') : null;
    if (chip) {
      plFilter = {mode: chip.dataset.filter, gang: chip.dataset.gang || null};
      document.querySelectorAll('.pl-chip').forEach(function (c) { c.classList.toggle('on', c === chip); });
      plShown = PL_PAGE;
      plRender();
      return;
    }
    if (e.target.closest && e.target.closest('#pl-more-btn')) {
      plShown += PL_PAGE;
      plRender();
    }
  });
  // One fat cycle button instead of a native select — two precise taps plus a
  // system picker overlay are unusable in a moving car. Delegated: the button is
  // replaced by every applyLive planner swap.
  var PL_SORTS = ['dist', 'easy', 'aps', 'worth'];
  function sortLabel(s) { return T['sort_' + s] || s; }
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('#pl-sort') : null;
    if (!b) return;
    plSort = PL_SORTS[(PL_SORTS.indexOf(plSort) + 1) % PL_SORTS.length];
    b.textContent = sortLabel(plSort);
    plShown = PL_PAGE;
    plRender();
  });

  // ---- Bottom sheet: the panel snaps between peek/half/full (UI redesign step 4) ----
  // Tap-to-cycle only (drag physics deferred). The MAP tab lowers to peek; content
  // tabs raise a peeked sheet to half. Snap height changes the map container size, so
  // every snap calls map.invalidateSize() (twice: now + after the CSS transition).
  // applyLive still innerHTML-swaps #planner-body/#watcher-body/#info-grid untouched —
  // this only moves whole .tabc/.panel containers, never their IDs.
  var panelEl = document.querySelector('.panel');
  var currentSnap = 'half';
  var activeContent = (initTab && initTab !== 'planer') ? initTab : 'planer';
  function renderTabsActive() {
    var mark = currentSnap === 'peek' ? 'map' : activeContent;
    document.querySelectorAll('.tab').forEach(function (x) {
      x.classList.toggle('active', x.dataset.tab === mark);
    });
  }
  function invalidateSoon() {
    map.invalidateSize();
    setTimeout(function () { map.invalidateSize(); }, 240);   // after the height transition
  }
  function setSnap(s) {
    currentSnap = s;
    panelEl.classList.remove('snap-peek', 'snap-half', 'snap-full');
    panelEl.classList.add('snap-' + s);
    renderTabsActive();
    invalidateSoon();
  }
  function showContent(name) {
    activeContent = name;
    document.querySelectorAll('.tabc').forEach(function (c) { c.hidden = c.dataset.tabc !== name; });
    if (currentSnap === 'peek') setSnap('half');
    else { renderTabsActive(); invalidateSoon(); }
    if (name === 'planer') maybeCoach();
  }
  document.querySelectorAll('.tab').forEach(function (t) {
    t.addEventListener('click', function () {
      if (t.dataset.tab === 'map') setSnap('peek');
      else showContent(t.dataset.tab);
    });
  });
  // Drag the sheet with the finger (pointer events = touch + mouse); on release it
  // snaps to the nearest of peek/half/full. A near-motionless press falls back to a
  // tap that cycles up. During the drag the CSS height transition is off (.dragging)
  // so the sheet tracks 1:1; map.invalidateSize() runs once on release via setSnap.
  var grab = document.getElementById('sheet-grab');
  function snapPx() {
    var vh = window.innerHeight;
    return {peek: 0.13 * vh, half: 0.45 * vh, full: 0.88 * vh};
  }
  function nearestSnap(px) {
    var s = snapPx(), best = 'half', bd = Infinity;
    ['peek', 'half', 'full'].forEach(function (k) {
      var d = Math.abs(px - s[k]); if (d < bd) { bd = d; best = k; }
    });
    return best;
  }
  if (grab) {
    var dragging = false, startY = 0, startH = 0, moved = 0;
    grab.addEventListener('pointerdown', function (e) {
      dragging = true; moved = 0; startY = e.clientY;
      startH = panelEl.getBoundingClientRect().height;
      panelEl.classList.add('dragging');
      try { grab.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    grab.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      var dy = startY - e.clientY;               // drag up = taller
      moved = Math.max(moved, Math.abs(dy));
      var s = snapPx();
      panelEl.style.height = Math.max(s.peek, Math.min(s.full, startH + dy)) + 'px';
    });
    function endDrag() {
      if (!dragging) return;
      dragging = false;
      panelEl.classList.remove('dragging');
      if (moved < 8) {                            // a tap → cycle up
        panelEl.style.height = '';
        var order = ['peek', 'half', 'full'];
        setSnap(order[(order.indexOf(currentSnap) + 1) % order.length]);
        return;
      }
      var target = nearestSnap(panelEl.getBoundingClientRect().height);
      setSnap(target);                            // add the snap class...
      panelEl.style.height = '';                  // ...then hand height back to it (animates)
    }
    grab.addEventListener('pointerup', endDrag);
    grab.addEventListener('pointercancel', endDrag);
  }

  // Planner/tour how-to as a ONE-TIME coach toast instead of two permanent hint
  // paragraphs — fires the first time the planner is seen, then never again.
  function maybeCoach() {
    var seen;
    try { seen = localStorage.getItem('wr_hints_seen'); } catch (e) {}
    if (seen) return;
    try { localStorage.setItem('wr_hints_seen', '1'); } catch (e) {}
    toast(T.planner_hint + '<br>' + T.tour_empty, 8000);
  }

  // Initial state: show the landing tab's content at half height.
  document.querySelectorAll('.tabc').forEach(function (c) { c.hidden = c.dataset.tabc !== activeContent; });
  setSnap('half');
  if (activeContent === 'planer') setTimeout(maybeCoach, 1200);

  // ---- Follow mode: own live position + "you are here" context ----
  var meMarker = null, meCircle = null, follow = false, watchId = null;
  var here = document.getElementById('here');
  function freshTag() {
    var mins = Math.floor((Date.now() - dataAt) / 60000);
    var cls = mins < 7 ? 'ok' : 'old';   // ~ one poll cycle + slack
    var txt = mins < 1 ? T.hb_live : mins + 'm';
    return ' <span class="hb-fresh ' + cls + '">◈ ' + txt + '</span>';
  }
  function updateHere(lat, lng) {
    if (guidanceOn || (nextmoveOn && nmCard && !nmCard.hidden)) { here.hidden = true; return; }   // nav strip / next-move card own the bottom slot
    var c = cellAt(lat, lng);
    here.hidden = false;
    var cls, html;
    if (!c) {
      // No feed entry: either virgin land (never scanned → grab it!) or outside
      var k = Math.floor(lat / grid.lat + 1e-9) + '_' + Math.floor(lng / grid.lng + 1e-9);
      if (virginByKey[k]) { cls = 'here-banner virgin'; html = T.here_virgin; }
      else { cls = 'here-banner out'; html = T.here_out; }
    } else if (c.status === 'mine') {
      cls = 'here-banner mine'; html = tf(T.here_mine, {n: (c.my_aps || 0)});
    } else if (c.status === 'free') {
      cls = 'here-banner free'; html = tf(T.here_free, {n: (c.my_aps || 0)});
    } else {
      cls = 'here-banner enemy';
      html = c.gap == null ? tf(T.here_enemy_fog, {g: esc(c.gang)})
           : c.gap === 0 ? tf(T.here_enemy_lead, {g: esc(c.gang)})
                         : tf(T.here_enemy_gap, {g: esc(c.gang), n: c.gap});
    }
    here.className = cls;
    here.innerHTML = html + freshTag();   // append the data-freshness segment
  }
  function onPos(p) {
    var lat = p.coords.latitude, lng = p.coords.longitude, acc = p.coords.accuracy || 0, ll = [lat, lng];
    if (!meMarker) {
      meMarker = L.marker(ll, {icon: L.divIcon({className: 'me-dot', iconSize: [18, 18],
        iconAnchor: [9, 9], html: '<span></span>'}), zIndexOffset: 1000}).addTo(map);
      meCircle = L.circle(ll, {radius: acc, color: '#3b82f6', weight: 1, fillOpacity: 0.08}).addTo(map);
    } else { meMarker.setLatLng(ll); meCircle.setLatLng(ll).setRadius(acc); }
    if (follow) map.setView(ll, Math.max(map.getZoom(), 13));
    updateHere(lat, lng);
    maybePush(lat, lng);
    navUpdate(lat, lng);
    plOnPosition();  // keep distances updated; re-sorting only throttled
    if (ringsOn) renderRings();   // rings follow the moving GPS position
    if (nextmoveOn) renderNextmove();
    if (recOn) covRecord(lat, lng, acc);   // brush the covered ground while recording
  }
  function onPosErr() {
    if (guidanceOn) return;   // the nav strip owns the bottom slot; don't flash a GPS-error banner over it
    here.hidden = false; here.className = 'here-banner out'; here.innerHTML = T.no_gps;
  }
  var LocateCtl = L.Control.extend({options: {position: 'bottomright'}, onAdd: function () {
    var d = L.DomUtil.create('div', 'leaflet-bar locate-ctl');
    d.innerHTML = '<a href="#" id="loc-btn" title="Follow" role="button">◎</a>';
    L.DomEvent.disableClickPropagation(d);
    return d;
  }});
  map.addControl(new LocateCtl());
  document.getElementById('loc-btn').addEventListener('click', function (e) {
    e.preventDefault();
    var btn = document.getElementById('loc-btn');
    if (!navigator.geolocation) { onPosErr(); return; }
    if (!watchId) {
      watchId = navigator.geolocation.watchPosition(onPos, onPosErr,
        {enableHighAccuracy: true, maximumAge: 5000, timeout: 15000});
      follow = true; btn.classList.add('active');
    } else {
      follow = !follow; btn.classList.toggle('active', follow);
      if (follow && meMarker) map.setView(meMarker.getLatLng(), Math.max(map.getZoom(), 13));
    }
    void btn.offsetWidth;  // otherwise iOS paints class changes on Leaflet controls only on the next reflow
  });

  // ---- Manual location: drag the map under the centre crosshair, confirm ----
  // For planning from the couch or when GPS is off/indoors. Sets the same meMarker
  // that GPS would, so the here-banner, planner distances and tour guidance all use it.
  var SetLocCtl = L.Control.extend({options: {position: 'bottomright'}, onAdd: function () {
    var d = L.DomUtil.create('div', 'leaflet-bar locate-ctl');
    d.innerHTML = '<a href="#" id="setloc-btn" title="' + esc(T.loc_set_title) + '" role="button">⌖</a>';
    L.DomEvent.disableClickPropagation(d);
    return d;
  }});
  map.addControl(new SetLocCtl());
  function locModeShow(on) {
    document.getElementById('loc-cross').hidden = !on;
    document.getElementById('loc-setbar').hidden = !on;
    var hero = document.getElementById('map-hero');
    if (hero) hero.style.visibility = on ? 'hidden' : (guidanceOn ? 'hidden' : '');
    var b = document.getElementById('setloc-btn'); if (b) b.classList.toggle('active', on);
    // Exclusive mode (step 8): dim the whole app to the crosshair + confirm bar so
    // positioning is undistracted. Hiding the sheet grows the map → invalidateSize.
    document.body.classList.toggle('wr-setloc', on);
    map.invalidateSize();
    setTimeout(function () { map.invalidateSize(); }, 60);
  }
  function setManualPos(lat, lng) {
    if (watchId) { navigator.geolocation.clearWatch(watchId); watchId = null; }
    follow = false;
    var lb = document.getElementById('loc-btn'); if (lb) lb.classList.remove('active');
    var ll = [lat, lng];
    if (!meMarker) {
      meMarker = L.marker(ll, {icon: L.divIcon({className: 'me-dot manual', iconSize: [18, 18],
        iconAnchor: [9, 9], html: '<span></span>'}), zIndexOffset: 1000}).addTo(map);
    } else { meMarker.setLatLng(ll); }
    if (meCircle) { map.removeLayer(meCircle); meCircle = null; }  // no accuracy circle for a manual pin
    updateHere(lat, lng);
    navUpdate(lat, lng);
    plOnPosition();
    if (ringsOn) renderRings();   // re-center rings on the new manual pin
    if (nextmoveOn) renderNextmove();
  }
  document.getElementById('setloc-btn').addEventListener('click', function (e) {
    e.preventDefault();
    if (meMarker) map.setView(meMarker.getLatLng(), Math.max(map.getZoom(), 12));
    locModeShow(true);
  });
  document.getElementById('loc-cancel').addEventListener('click', function () { locModeShow(false); });
  document.getElementById('loc-confirm').addEventListener('click', function () {
    var c = map.getCenter();
    setManualPos(c.lat, c.lng);
    locModeShow(false);
  });

  // ---- Distance rings ("radar"): equidistant circles around your position ----
  // For eyeballing distances. Geographic radii (L.circle in meters) scale with
  // zoom by themselves; on zoomend we only re-pick the step from a 1-2-5 series
  // so ~4 evenly spaced rings stay in view. Centered on myPos() (GPS follow or
  // the manual ⌖ pin) and re-centered whenever the position moves. Pure client
  // layer — applyLive swaps never touch it.
  var ringLayer = L.layerGroup();
  var ringsOn = false;
  var RING_N = 4, RING_TARGET_PX = 95;
  // Own 1-2-5 series per unit system: converting 2 km to 1.24 mi would make the
  // labels useless — imperial users get rings on clean mile values instead.
  var RING_STEPS_M = [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000];
  var RING_STEPS_MI = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100].map(
    function (m) { return m * 1609.344; });
  function ringStep() {
    var steps = units === 'mi' ? RING_STEPS_MI : RING_STEPS_M;
    var c = map.getCenter();
    var mpp = 40075016.686 * Math.abs(Math.cos(c.lat * Math.PI / 180)) /
              Math.pow(2, map.getZoom() + 8);   // meters per screen pixel
    var raw = mpp * RING_TARGET_PX;
    for (var i = 0; i < steps.length; i++) if (steps[i] >= raw) return steps[i];
    return steps[steps.length - 1];
  }
  function ringFmt(r) {   // r in meters
    if (units === 'mi') return (Math.round(r / 1609.344 * 10) / 10) + ' mi';
    return r < 1000 ? r + ' m' : (Math.round(r / 100) / 10) + ' km';
  }
  function renderRings() {
    ringLayer.clearLayers();
    if (!ringsOn) return;
    var p = myPos();
    if (!p) return;
    var s = ringStep();
    for (var k = 1; k <= RING_N; k++) {
      var r = s * k;
      // Near-white, NOT gold: own cells glow gold, so gold rings vanished on top
      // of them. White reads on dark tiles, gold, red and frost alike.
      L.circle([p.lat, p.lng], {radius: r, color: '#f2f6fa', weight: 1.5, opacity: 0.7,
        fill: false, dashArray: '4 6', interactive: false}).addTo(ringLayer);
      // Label at the ring's north point; non-interactive so cell popups stay clickable.
      L.marker([p.lat + r / 111320, p.lng], {interactive: false, keyboard: false,
        icon: L.divIcon({className: 'ring-label', iconSize: [64, 14], iconAnchor: [32, 7],
          html: ringFmt(r)})}).addTo(ringLayer);
    }
  }
  map.on('zoomend', renderRings);   // cheap: 4 circles + 4 labels, no need to diff the step
  // Toggle called from the ◈ layers popover. Needs a position to center on.
  function toggleRings() {
    if (!ringsOn && !myPos()) {   // no position yet → explain instead of a dead toggle
      here.hidden = false; here.className = 'here-banner out'; here.innerHTML = T.rings_need_pos;
      setTimeout(function () { if (!myPos()) here.hidden = true; }, 3000);
      return;
    }
    ringsOn = !ringsOn;
    if (ringsOn) { ringLayer.addTo(map); renderRings(); }
    else { ringLayer.clearLayers(); map.removeLayer(ringLayer); }
    if (typeof reflectLayers === 'function') reflectLayers();
  }

  // ---- "Next move" card (step 11, opt-in via the ⚡ rail toggle) ----
  // One suggested next action in the bottom slot when no tour navigation runs:
  // escalated watcher events (attack on your turf) first, then the nearest flip
  // target. Client-side from data already present; tap the body to fly there, ↻ cycles.
  var nextmoveOn = false, nmIdx = 0;
  var nmCard = document.getElementById('nextmove');
  var nmBody = document.getElementById('nm-body');
  var nmSkip = document.getElementById('nm-skip');
  function nmCandidates() {
    var list = [];
    var wb = document.getElementById('watcher-body');
    if (wb) {
      wb.querySelectorAll('.ev.prox-mine[data-lat], .ev.prox-gang[data-lat]').forEach(function (el) {
        var m = el.querySelector('.ev-motto');
        list.push({lat: +el.dataset.lat, lng: +el.dataset.lng, kind: 'attack',
                   label: m ? m.textContent.trim() : T.nm_attack});
      });
    }
    var mp = myPos();
    if (mp) {   // nearest flip target — enemy/free only (bounded; virgin land is thousands)
      var best = null, bd = Infinity;
      targets.forEach(function (c) {
        if (c.lat == null) return;
        var d = hav(mp, {lat: c.lat, lng: c.lng});
        if (d < bd) { bd = d; best = c; }
      });
      if (best) list.push({lat: best.lat, lng: best.lng, kind: 'target',
        label: best.t === 'free' ? T.nm_free : (best.g || T.nm_target)});
    }
    return list;
  }
  function renderNextmove() {
    if (!nmCard) return;
    if (!nextmoveOn || guidanceOn) { nmCard.hidden = true; return; }
    var cand = nmCandidates();
    nmCard.hidden = false;
    if (!cand.length) {
      nmCard.className = 'nextmove-card empty';
      nmBody.textContent = T.nm_none;
      nmBody.removeAttribute('data-lat');
      if (nmSkip) nmSkip.hidden = true;
      return;
    }
    if (nmIdx >= cand.length) nmIdx = 0;
    var c = cand[nmIdx], mp = myPos();
    var dist = mp ? fmtDist(hav(mp, {lat: c.lat, lng: c.lng})) : '';
    nmCard.className = 'nextmove-card ' + c.kind;
    nmBody.innerHTML = '<span class="nm-icon">' + (c.kind === 'attack' ? '⚔' : '◇') + '</span> ' +
      esc(c.label) + (dist ? ' <span class="nm-dist">· ' + dist + '</span>' : '');
    nmBody.dataset.lat = c.lat; nmBody.dataset.lng = c.lng;
    if (nmSkip) nmSkip.hidden = cand.length <= 1;
  }
  var nmToggle = document.getElementById('nextmove-toggle');
  if (nmToggle) nmToggle.addEventListener('click', function () {
    nextmoveOn = !nextmoveOn; nmIdx = 0;
    nmToggle.classList.toggle('on', nextmoveOn);
    renderNextmove();
    var mp = myPos(); if (mp) updateHere(mp.lat, mp.lng);   // here-banner yields / returns
  });
  if (nmBody) nmBody.addEventListener('click', function () {
    if (nmBody.dataset.lat) jumpToEvent(+nmBody.dataset.lat, +nmBody.dataset.lng);
  });
  if (nmSkip) nmSkip.addEventListener('click', function () { nmIdx++; renderNextmove(); });

  // ---- Coverage brush: log the ground actually driven, not just cells with APs ----
  // While recording, each GPS fix drops a disc of the operator's expected reception
  // radius; the union of discs is a live brush stroke of covered ground. Discs are
  // geographic L.circle on a dedicated canvas renderer, so they auto-scale with zoom
  // and thousands stay cheap. Points are buffered to localStorage and flushed to
  // /coverage in batches — wardriving means dead zones, so a failed POST just waits.
  var COV_RADII = [50, 100, 150, 200, 300];   // metres; cycle in the popover
  var COV_COL = {gps: '#2ee6a6', ap: '#5aa9e6'};   // gps = driven; ap = future backfill
  var COV_OP = {gps: 0.16, ap: 0.10};
  var covRenderer = L.canvas({padding: 0.5});
  var covGroup = L.layerGroup();
  var covOn = false, recOn = false, covFlushing = false;
  var covPts = [], covQueue = [], covLast = null, recRadius = 100;
  var hasVirgin = !!(DATA.virginAll && DATA.virginAll.length);
  try { var rr = parseInt(localStorage.getItem('wr_cov_radius'), 10); if (rr) recRadius = rr; } catch (e) {}
  try { covQueue = JSON.parse(localStorage.getItem('wr_cov_queue') || '[]') || []; } catch (e) { covQueue = []; }
  var wasRec = false;
  try { wasRec = localStorage.getItem('wr_cov_rec') === '1'; } catch (e) {}   // were we recording when the page went away?

  function fmtRadius(m) {
    return units === 'mi' ? (Math.round(m * 3.28084 / 10) * 10) + ' ft' : m + ' m';
  }
  function addCovDisc(p) {
    var src = p.src || 'gps';
    L.circle([p.lat, p.lng], {radius: p.r, stroke: false, fillColor: COV_COL[src] || COV_COL.gps,
      fillOpacity: COV_OP[src] || COV_OP.gps, interactive: false, renderer: covRenderer}).addTo(covGroup);
  }
  function renderCoverage() {
    covGroup.clearLayers();
    if (!covOn) return;
    covPts.forEach(addCovDisc);
  }
  function toggleCoverage() {
    covOn = !covOn;
    if (covOn) { covGroup.addTo(map); renderCoverage(); }
    else { covGroup.clearLayers(); map.removeLayer(covGroup); }
    reflectLayers();
  }

  function covPersist() {
    try { localStorage.setItem('wr_cov_queue', JSON.stringify(covQueue)); } catch (e) {}
  }
  // keepalive=true lets the request survive the page being backgrounded/closed (unload
  // safety net). The queue is only cleared on a confirmed .then, so if the page is
  // discarded mid-flight the points re-flush on reopen — server OR IGNORE dedups them.
  function covFlush(keepalive) {
    if (covFlushing || !covQueue.length) return;
    covFlushing = true;
    var batch = covQueue.slice(0, 500);
    var opts = {method: 'POST', headers: {'Content-Type': 'application/json',
      'X-Requested-With': 'fetch'}, body: JSON.stringify({pts: batch})};
    if (keepalive) opts.keepalive = true;
    fetch('/coverage', opts)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (res) {
        if (res && res.ok) { covQueue.splice(0, batch.length); covPersist(); }
      }).catch(function () {}).finally(function () { covFlushing = false; });
  }
  // Called from onPos on every GPS fix while recording. Throttled by distance so a
  // parked car doesn't pile up discs on one spot; wild fixes (poor accuracy) are dropped.
  function covRecord(lat, lng, acc) {
    if (acc > 0 && acc > 150) return;
    var here = {lat: lat, lng: lng};
    if (covLast && hav(covLast, here) * 1000 < recRadius / 2) return;   // hav → km
    covLast = here;
    var p = {lat: lat, lng: lng, r: recRadius, src: 'gps', t: new Date().toISOString()};
    covPts.push(p); covQueue.push(p); covPersist();
    if (covOn) addCovDisc(p);
    if (covQueue.length % 20 === 0) covFlush();   // trickle to the server; no-op when offline
  }
  // Screen Wake Lock: a web app cannot log GPS in the background (the browser suspends
  // hidden tabs), so the next best thing is to keep the screen awake while recording so
  // the tab stays foreground and the position stream keeps firing. The lock is auto-
  // released when the page hides, so it is re-acquired on visibilitychange -> visible.
  var wakeLock = null;
  function acquireWakeLock() {
    if (!('wakeLock' in navigator) || wakeLock) return;
    navigator.wakeLock.request('screen').then(function (wl) {
      wakeLock = wl;
      wl.addEventListener('release', function () { wakeLock = null; });
    }).catch(function () {});   // rejects if not visible / not permitted — harmless
  }
  function releaseWakeLock() {
    if (wakeLock) { wakeLock.release().catch(function () {}); wakeLock = null; }
  }
  var recPill = document.getElementById('rec-pill');
  function reflectRec() {
    if (recPill) recPill.hidden = !recOn;
    var btn = document.getElementById('cov-rec');
    if (btn) { btn.textContent = (recOn ? '■ ' + T.cov_stop : '● ' + T.cov_rec); btn.classList.toggle('on', recOn); }
    var lb = document.getElementById('layers-btn');
    if (lb) lb.classList.toggle('recording', recOn);
  }
  function recStart(resumed) {
    if (!navigator.geolocation) { toast('<b>' + esc(T.cov_need_gps) + '</b>', 4000); return; }
    if (!watchId) {   // recording implies GPS follow — start it like the ◎ button does
      watchId = navigator.geolocation.watchPosition(onPos, onPosErr,
        {enableHighAccuracy: true, maximumAge: 5000, timeout: 15000});
      follow = true; var lb = document.getElementById('loc-btn'); if (lb) lb.classList.add('active');
    }
    recOn = true; covLast = null;
    try { localStorage.setItem('wr_cov_rec', '1'); } catch (e) {}   // survive reload/close -> auto-resume
    acquireWakeLock();
    if (!covOn) toggleCoverage();   // show the brush as it grows
    reflectRec();
    toast('<b>' + esc(resumed ? T.cov_resumed : T.cov_started) + '</b>', 3500);
  }
  function recStop() {
    recOn = false;
    try { localStorage.removeItem('wr_cov_rec'); } catch (e) {}
    releaseWakeLock();
    reflectRec();
    covFlush();
  }
  if (recPill) recPill.addEventListener('click', recStop);

  // ◈ layers control: virgin / distance rings / coverage moved off the rail into one
  // popover on the map's control column (bottom-right, next to zoom/locate/info).
  var LayersCtl = L.Control.extend({options: {position: 'bottomright'}, onAdd: function () {
    var d = L.DomUtil.create('div', 'leaflet-bar layers-ctl');
    var rows = '';
    if (hasVirgin) rows += '<button type="button" class="lyr-row" data-layer="virgin">' +
      '<span class="lyr-ic">◇</span>' + esc(T.lyr_virgin) + '</button>';
    rows += '<button type="button" class="lyr-row" data-layer="rings">' +
      '<span class="lyr-ic">⊚</span>' + esc(T.lyr_rings) + '</button>';
    rows += '<button type="button" class="lyr-row" data-layer="masts">' +
      '<span class="lyr-ic">📡</span>' + esc(T.lyr_masts) + '</button>';
    rows += '<div class="lyr-sep"></div>' +
      '<button type="button" class="lyr-row" data-layer="cov"><span class="lyr-ic">▨</span>' + esc(T.lyr_cov) + '</button>' +
      '<div class="cov-ctrls">' +
        '<div class="cov-line"><span>' + esc(T.cov_radius) + '</span>' +
          '<button type="button" class="cov-radius" id="cov-radius">' + fmtRadius(recRadius) + '</button></div>' +
        '<div class="cov-line"><button type="button" class="cov-rec" id="cov-rec">● ' + esc(T.cov_rec) + '</button>' +
          '<button type="button" class="cov-clear" id="cov-clear">↺ ' + esc(T.cov_clear) + '</button></div>' +
      '</div>';
    d.innerHTML = '<div id="layers-box" class="layers-box" hidden>' +
      '<div class="layers-head">' + esc(T.layers_title) + '</div>' + rows + '</div>' +
      '<a href="#" id="layers-btn" role="button" title="' + esc(T.layers_title) + '">&#9672;</a>';
    L.DomEvent.disableClickPropagation(d);
    L.DomEvent.disableScrollPropagation(d);
    return d;
  }});
  map.addControl(new LayersCtl());
  function reflectLayers() {
    var box = document.getElementById('layers-box');
    if (!box) return;
    var st = {virgin: virginOn, rings: ringsOn, masts: mastsOn, cov: covOn};
    box.querySelectorAll('.lyr-row').forEach(function (row) {
      row.classList.toggle('on', !!st[row.dataset.layer]);
    });
    var rb = document.getElementById('cov-radius'); if (rb) rb.textContent = fmtRadius(recRadius);
    reflectRec();
  }
  document.getElementById('layers-btn').addEventListener('click', function (e) {
    e.preventDefault();
    var b = document.getElementById('layers-box');
    b.hidden = !b.hidden;
    if (!b.hidden) reflectLayers();
  });
  map.on('click', function () {
    var b = document.getElementById('layers-box');
    if (b && !b.hidden) b.hidden = true;
  });
  document.querySelectorAll('.lyr-row').forEach(function (row) {
    row.addEventListener('click', function () {
      var l = row.dataset.layer;
      if (l === 'virgin') toggleVirgin();
      else if (l === 'rings') toggleRings();
      else if (l === 'masts') toggleMasts();
      else if (l === 'cov') toggleCoverage();
    });
  });
  document.getElementById('cov-radius').addEventListener('click', function () {
    var i = COV_RADII.indexOf(recRadius);
    recRadius = COV_RADII[(i + 1) % COV_RADII.length];
    try { localStorage.setItem('wr_cov_radius', String(recRadius)); } catch (e) {}
    this.textContent = fmtRadius(recRadius);
  });
  document.getElementById('cov-rec').addEventListener('click', function () {
    if (recOn) recStop(); else recStart();
  });
  document.getElementById('cov-clear').addEventListener('click', function () {
    if (!window.confirm(T.cov_clear_ask)) return;
    fetch('/coverage/clear', {method: 'POST', headers: {'X-Requested-With': 'fetch'}})
      .then(function () {
        covPts = []; covQueue = []; covLast = null; covPersist(); renderCoverage();
      }).catch(function () {});
  });

  // Load stored coverage; seed the render buffer with any unflushed local points so a
  // reload mid-drive doesn't lose the tail, then flush the leftover queue.
  fetch('/coverage.json', {headers: {'X-Requested-With': 'fetch'}})
    .then(function (r) { return r.ok ? r.json() : {pts: []}; })
    .then(function (d) {
      covPts = (d.pts || []).concat(covQueue);
      if (covOn) renderCoverage();
    }).catch(function () {});
  covFlush();
  setInterval(function () { covFlush(); }, 30000);

  // Resume where an accidental reload/close left off: if we were recording, pick it
  // straight back up (GPS permission is already granted, so no new prompt).
  if (wasRec) recStart(true);

  // Lifecycle: get buffered points out before the tab is suspended/closed, and re-arm
  // the wake lock (auto-released on hide) plus drain the buffer when we come back.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      covFlush(true);   // keepalive so it survives the suspend/close
    } else {
      covFlushing = false;   // clear a flag that may be stuck from a frozen flush
      if (recOn) { acquireWakeLock(); covFlush(); }
    }
  });
  window.addEventListener('pagehide', function () { covFlush(true); });

  // ---- Crew: friends' live positions (12s poll) + own push while sharing ----
  var friendLayer = L.layerGroup().addTo(map);
  function loadFriends() {
    fetch('/friends/positions.json', {headers: {'X-Requested-With': 'fetch'}})
      .then(function (r) { return r.ok ? r.json() : {friends: []}; })
      .then(function (d) {
        friendLayer.clearLayers();
        (d.friends || []).forEach(function (f) {
          var ago = f.age_s < 60 ? T.friend_now : tf(T.friend_ago, {n: Math.round(f.age_s / 60)});
          L.marker([f.lat, f.lng], {zIndexOffset: 800, icon: L.divIcon({className: 'friend-dot',
            iconSize: [16, 16], iconAnchor: [8, 8], html: '<span></span>'})}).addTo(friendLayer)
            .bindPopup('<b>' + esc(f.username) + '</b>' + (f.gang ? ' · ' + esc(f.gang) : '') + '<br>' + ago);
        });
        if (d.last_poll && String(d.last_poll) !== String(POLL_EPOCH)) applyLive();
      }).catch(function () {});
  }
  loadFriends();
  setInterval(loadFriends, 12000);
  // Keep the here-banner's freshness tag climbing even when parked (no GPS ticks).
  setInterval(function () {
    var mp = myPos();
    if (mp && !here.hidden && !guidanceOn) updateHere(mp.lat, mp.lng);
  }, 60000);

  // ---- Live update without reloading ----
  // If the poller has fresher data, we patch map, counters, watcher and planner
  // in-place. No reload (no flashing, no state loss) — which is why it also runs
  // during tour guidance: right during an attack the watcher has to get through.
  var applying = false;
  function applyLive() {
    if (applying) return;
    applying = true;
    fetch('/api/live', {headers: {'X-Requested-With': 'fetch'}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        POLL_EPOCH = d.poll;
        dataAt = Date.now();   // fresh poll data landed → reset the freshness clock

        var ig = document.getElementById('info-grid');
        if (ig && d.info_html != null) ig.innerHTML = d.info_html;

        var wb = document.getElementById('watcher-body');
        if (wb && d.watcher_html != null) wb.innerHTML = d.watcher_html;
        var badge = document.getElementById('watch-badge');
        if (badge) { badge.textContent = d.events_n; badge.hidden = !d.events_n; }

        if (d.virgin) { virginCells = expandVirgin(d.virgin); indexVirgin(); renderVirgin(); virginSnapped = false; if (virginOn) snapVirginWater(); }
        if (d.targets) targets = d.targets;

        var pb = document.getElementById('planner-body');
        if (pb && d.planner_html != null) {
          pb.innerHTML = d.planner_html;   // only chips + sort field
          // Restore the user's filter/sorting after the swap (cycle-button label)
          var sel = document.getElementById('pl-sort');
          if (sel) sel.textContent = sortLabel(plSort);
          var known = false;
          document.querySelectorAll('.pl-chip').forEach(function (c) {
            var same = c.dataset.filter === plFilter.mode &&
                       (c.dataset.gang || null) === plFilter.gang;
            c.classList.toggle('on', same);
            if (same) known = true;
          });
          // The filtered gang may have vanished (cell flipped) → back to All
          if (!known) {
            plFilter = {mode: 'all', gang: null};
            var all = document.querySelector('.pl-chip[data-filter="all"]');
            if (all) all.classList.add('on');
          }
          plRender();   // plShown stays: whoever clicked "more" keeps their list
        }

        if (d.counts) {
          ['mine', 'enemy', 'free'].forEach(function (k) {
            var el = document.querySelector('.rb.' + k + ' b');
            if (el) el.textContent = d.counts[k];
          });
        }
        // Only redraw cells when no popup is open (redrawing would close it).
        // Map view/zoom stay untouched — we only redraw the layer.
        if (d.cells && !document.querySelector('.leaflet-popup')) {
          cells = d.cells;
          renderCells();
          if (mastsOn) renderMasts();   // tower counts ride on the same cells
          var mp = myPos();
          if (mp && !here.hidden) updateHere(mp.lat, mp.lng);
        }
        if (nextmoveOn) renderNextmove();   // fresh events/targets → refresh the suggestion
      })
      .catch(function () {})
      .then(function () { applying = false; });
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') loadFriends();
  });

  var lastPush = 0;
  function maybePush(lat, lng) {
    if (!SHARING) return;
    var now = Date.now();
    if (now - lastPush < 12000) return;
    lastPush = now;
    fetch('/position', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'lat=' + encodeURIComponent(lat) + '&lng=' + encodeURIComponent(lng)}).catch(function () {});
  }

  // ---- Dashboard sparklines from the stats time series ----
  (function () {
    if (!HISTORY || HISTORY.length < 2) return;
    var box = document.getElementById('charts');
    if (!box) return;
    function spark(vals, color, invert) {
      var nums = vals.filter(function (v) { return v !== null && v !== undefined; });
      if (nums.length < 2) return '';
      var min = Math.min.apply(null, nums), max = Math.max.apply(null, nums), rng = (max - min) || 1;
      var pts = vals.map(function (v, i) {
        var norm = ((v == null ? min : v) - min) / rng; if (invert) norm = 1 - norm;
        return ((i / (vals.length - 1)) * 100).toFixed(1) + ',' + (28 - norm * 26).toFixed(1);
      });
      return '<svg viewBox="0 0 100 30" preserveAspectRatio="none" class="spark"><polyline points="' +
        pts.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="1.5"/></svg>';
    }
    function row(label, val, svg) {
      return '<div class="chart-row"><div class="chart-lbl">' + label + ' <b>' + val + '</b></div>' + svg + '</div>';
    }
    var last = HISTORY[HISTORY.length - 1], html = '';
    html += row(T.chart_total, last.total != null ? last.total : '—',
                spark(HISTORY.map(function (h) { return h.total; }), '#e8b64c', false));
    html += row(T.chart_rank, last.gang_rank != null ? '#' + last.gang_rank : '—',
                spark(HISTORY.map(function (h) { return h.gang_rank; }), '#8fa7bd', true));
    html += row(T.chart_cl,
                (last.team_captured != null ? last.team_captured : '—') + ' / ' + (last.team_lost != null ? last.team_lost : '—'),
                spark(HISTORY.map(function (h) { return (h.team_captured || 0) - (h.team_lost || 0); }), '#d63b41', false));
    box.innerHTML = html;
  })();

  // ---- Loot tour: pick targets → optimize the order → guide ----
  var tour = [];
  try { tour = JSON.parse(localStorage.getItem('wr_tour') || '[]') || []; } catch (e) { tour = []; }
  var tourOrdered = null;
  // true after drag & drop: the hand-made order is authoritative until the
  // user explicitly taps "auto order" (or clears the tour)
  var tourManual = false;
  try { tourManual = localStorage.getItem('wr_tour_manual') === '1'; } catch (e) {}
  var tourLayer = L.layerGroup().addTo(map);
  var panel = document.getElementById('tour-panel');
  var tourList = document.getElementById('tour-list');
  var tourCount = document.getElementById('tour-count');
  var mapsLink = document.getElementById('tour-maps');
  var navBanner = document.getElementById('nav');
  var navBody = document.getElementById('nav-body');
  var navEnd = document.getElementById('nav-end');
  if (navEnd) navEnd.addEventListener('click', function () { stopGuidance(); });
  var guidanceOn = false, navIdx = 0;

  // ---- Snap waypoints onto the road ----
  // The cell center often sits in a forest/field/river → Google builds routes to
  // nowhere from that. We fetch the nearest road point per cell (server-side
  // from OpenStreetMap, cached permanently). The cell identity (lat/lng = center)
  // stays untouched — the "+" matching and the auto-advance depend on it.
  function cellOf(s) {
    return [Math.floor(s.lat / grid.lat + 1e-9), Math.floor(s.lng / grid.lng + 1e-9)];
  }
  function stopPos(s) {
    return (s.rlat != null) ? {lat: s.rlat, lng: s.rlng} : {lat: s.lat, lng: s.lng};
  }
  // Batched: tapping several cells in quick succession fires ONE request instead of five.
  // Cells the server could not answer (Overpass momentarily down) stay
  // open and are retried — they must NOT count as "no road".
  var snapT = null, snapTries = 0;
  function snapTour() {
    if (snapT) clearTimeout(snapT);
    snapTries = 0;
    snapT = setTimeout(doSnap, 600);
  }
  function doSnap() {
    var need = tour.filter(function (s) { return s.rlat === undefined; });
    if (!need.length) return;
    fetch('/api/snap', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cells: need.map(cellOf)})})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.points) {
          tour.forEach(function (s) {
            var c = cellOf(s), k = c[0] + '_' + c[1];
            if (!(k in d.points)) return;        // unanswered → leave open
            var p = d.points[k];
            if (p) { s.rlat = p[0]; s.rlng = p[1]; s.noRoad = false; }
            else { s.rlat = null; s.rlng = null; s.noRoad = true; }
          });
          saveTour();
          renderTour();
          // Road points moved the stops — re-optimize, EXCEPT during guidance:
          // reordering under an active navIdx would silently retarget the driver.
          // Fetching the ROUTE is still safe there (geometry for the existing
          // order, no reordering) — without it, "Guide me" tapped before the
          // snap finished stayed on air lines for the whole drive. A hand-made
          // order is likewise never re-optimized, only re-routed.
          if (!guidanceOn && !tourManual && tour.length) optimize();
          else if (tourOrdered) { drawRoute(); fetchRoute(); }
        }
        // Cells still open? Try again later — Overpass is sometimes briefly gone.
        var open = tour.some(function (s) { return s.rlat === undefined; });
        if (open && snapTries < 3) {
          snapTries++;
          if (snapT) clearTimeout(snapT);
          snapT = setTimeout(doSnap, 6000 * snapTries);
        } else if (open) {
          // Overpass gave up for good — route via cell centers anyway, OSRM
          // snaps onto roads itself. Better an approximate real route than none.
          fetchRoute(true);
        }
      })
      .catch(function () {
        if (snapTries < 3) {
          snapTries++;
          if (snapT) clearTimeout(snapT);
          snapT = setTimeout(doSnap, 6000 * snapTries);
        } else {
          fetchRoute(true);   // same fallback on hard fetch failure
        }
      });
  }

  function tKey(lat, lng) { return (+lat).toFixed(4) + ',' + (+lng).toFixed(4); }
  function inTour(lat, lng) { var k = tKey(lat, lng); return tour.some(function (s) { return tKey(s.lat, s.lng) === k; }); }
  function saveTour() { try { localStorage.setItem('wr_tour', JSON.stringify(tour)); } catch (e) {} }
  function hav(a, b) {
    var R = 6371, dLa = (b.lat - a.lat) * Math.PI / 180, dLo = (b.lng - a.lng) * Math.PI / 180;
    var s = Math.sin(dLa / 2) * Math.sin(dLa / 2) + Math.cos(a.lat * Math.PI / 180) *
      Math.cos(b.lat * Math.PI / 180) * Math.sin(dLo / 2) * Math.sin(dLo / 2);
    return 2 * R * Math.asin(Math.sqrt(s));
  }
  function bearing(a, b) {
    var y = Math.sin((b.lng - a.lng) * Math.PI / 180) * Math.cos(b.lat * Math.PI / 180);
    var x = Math.cos(a.lat * Math.PI / 180) * Math.sin(b.lat * Math.PI / 180) - Math.sin(a.lat * Math.PI / 180) *
      Math.cos(b.lat * Math.PI / 180) * Math.cos((b.lng - a.lng) * Math.PI / 180);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }
  function myPos() {
    // Without a GPS marker there is no own position. (A never-defined variable
    // `own` used to sit here → ReferenceError that, without GPS, broke tour optimization among other things.)
    if (meMarker) { var ll = meMarker.getLatLng(); return {lat: ll.lat, lng: ll.lng}; }
    return null;
  }
  function cellKeyOf(lat, lng) { return Math.floor(lat / grid.lat + 1e-9) + '_' + Math.floor(lng / grid.lng + 1e-9); }

  function toggleTour(lat, lng, label) {
    lat = +lat; lng = +lng;
    var k = tKey(lat, lng), i = -1;
    tour.forEach(function (s, idx) { if (tKey(s.lat, s.lng) === k) i = idx; });
    var before = tour.length;
    if (i >= 0) tour.splice(i, 1); else tour.push({lat: lat, lng: lng, label: label || ''});
    // Notify exactly once when crossing the Google limit
    if (tour.length > MAPS_MAX && before <= MAPS_MAX) {
      toast('<b>' + esc(tf(T.maps_cap, {max: MAPS_MAX})) + '</b><br>' +
            esc(tf(T.maps_cap_toast, {max: MAPS_MAX, n: tour.length})), 6500, true);
    }
    tourOrdered = null; stopGuidance(); tourLayer.clearLayers(); saveTour(); renderTour();
    // Auto-optimize on every change: the Optimize button is gone. Unordered
    // tours silently exported an insertion-order route to Google Maps.
    // EXCEPT while a hand-dragged order is active — then the hand rules: new
    // stops append at the end, removals keep the rest untouched.
    if (tour.length) { if (tourManual) applyManualOrder(); else optimize(); }
    snapTour();
  }

  // Hand order (drag & drop): list order = drive order, no 2-opt on top.
  function applyManualOrder() {
    tourOrdered = tour.slice();
    routeGeo = null; routeKm = null;
    drawRoute(); renderTour();
    fetchRoute();
  }
  function setManual(on) {
    tourManual = on;
    try {
      if (on) localStorage.setItem('wr_tour_manual', '1');
      else localStorage.removeItem('wr_tour_manual');
    } catch (e) {}
  }

  function optimize() {
    if (!tour.length) return;
    var start = myPos() || stopPos(tour[0]), pts = tour.slice(), order = [], cur = start;
    while (pts.length) {
      var bi = 0, bd = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = hav(cur, stopPos(pts[i]));
        if (d < bd) { bd = d; bi = i; }
      }
      cur = stopPos(pts[bi]); order.push(pts[bi]); pts.splice(bi, 1);
    }
    function len(o) {
      var d = hav(start, stopPos(o[0]));
      for (var i = 1; i < o.length; i++) d += hav(stopPos(o[i - 1]), stopPos(o[i]));
      return d;
    }
    var improved = true, guard = 0;
    while (improved && guard++ < 80) {
      improved = false;
      for (var a = 0; a < order.length - 1; a++)
        for (var b = a + 1; b < order.length; b++) {
          var no = order.slice(0, a).concat(order.slice(a, b + 1).reverse(), order.slice(b + 1));
          if (len(no) + 1e-9 < len(order)) { order = no; improved = true; }
        }
    }
    tour = order; tourOrdered = order; saveTour();
    routeGeo = null; routeKm = null;   // stale geometry — refetch for the new order
    drawRoute(); renderTour();
    fetchRoute();
  }

  // ---- Real driving route (server-side OSRM proxy) ----
  // Sent out: the road-snapped stop points plus — when a position is set — the
  // route start, so the approach leg is real road too (disclosed on /about).
  // When no OSRM instance answers, everything falls back to the old dashed
  // straight lines and the total is marked approximate.
  var routeGeo = null, routeKm = null, routeFromPos = false, routeSeq = 0, routeT = null;
  function fetchRoute(force) {
    if (routeT) clearTimeout(routeT);
    routeT = setTimeout(function () {
      if (!tourOrdered || !tourOrdered.length) { routeGeo = null; routeKm = null; return; }
      // road-snapping still pending → the post-snap optimize will refetch with
      // the real road points; routing cell centers now would just be wasted.
      // force=true overrides: when Overpass has given up entirely, OSRM's own
      // snapping is better than never showing a route at all.
      if (!force && tour.some(function (s) { return s.rlat === undefined; })) return;
      var start = myPos();
      var pts = (start ? [[start.lat, start.lng]] : []).concat(
        tourOrdered.map(function (s) { var p = stopPos(s); return [p.lat, p.lng]; }));
      if (pts.length < 2) { routeGeo = null; routeKm = null; return; }
      var fromPos = !!start;
      var seq = ++routeSeq;
      fetch('/api/route', {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-Requested-With': 'fetch'}, body: JSON.stringify({pts: pts})})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (seq !== routeSeq) return;   // a newer tour superseded this request
          routeGeo = (d && d.ok && d.route) ? d.route.geometry : null;
          routeKm = (d && d.ok && d.route) ? d.route.km : null;
          routeFromPos = fromPos && !!routeGeo;
          drawRoute(); renderTour();
        }).catch(function () {});
    }, 600);
  }

  function totalKm() {
    if (!tourOrdered || !tourOrdered.length) return null;
    if (routeKm != null) return routeKm;   // real road km across the stops
    var start = myPos(), prev = start || stopPos(tourOrdered[0]), d = 0, i = start ? 0 : 1;
    for (; i < tourOrdered.length; i++) {
      var p = stopPos(tourOrdered[i]);
      d += hav(prev, p);
      prev = p;
    }
    return d;
  }

  function drawRoute() {
    tourLayer.clearLayers();
    if (!tourOrdered || !tourOrdered.length) return;
    var start = myPos();
    if (routeGeo && routeGeo.length) {
      // the real road, incl. the approach leg when the fetch had a position;
      // otherwise the me→route link stays a faint honest dash
      if (start && !routeFromPos) L.polyline([[start.lat, start.lng], routeGeo[0]],
        {color: '#e8b64c', weight: 2, opacity: 0.55, dashArray: '2 7', interactive: false}).addTo(tourLayer);
      L.polyline(routeGeo, {color: '#e8b64c', weight: 4, opacity: 0.9,
        className: 'route-real', interactive: false}).addTo(tourLayer);
    } else {
      var lls = (start ? [[start.lat, start.lng]] : []).concat(
        tourOrdered.map(function (s) { var p = stopPos(s); return [p.lat, p.lng]; }));
      L.polyline(lls, {color: '#e8b64c', weight: 3, opacity: 0.85, dashArray: '6 6', interactive: false}).addTo(tourLayer);
    }
    tourOrdered.forEach(function (s, i) {
      var p = stopPos(s);
      // Draggable: grab a numbered stop and move it ON THE MAP to shape the
      // route. The dropped point becomes the stop AND its routing point (no
      // re-snap yanking the hand-picked spot away); the label follows the new
      // location honestly.
      var m = L.marker([p.lat, p.lng], {zIndexOffset: 700, draggable: true,
        icon: L.divIcon({className: 'tour-num',
        iconSize: [22, 22], iconAnchor: [11, 11], html: '<span>' + (i + 1) + '</span>'})});
      m.on('dragend', function () {
        var ll = m.getLatLng();
        s.lat = ll.lat; s.lng = ll.lng;
        s.rlat = ll.lat; s.rlng = ll.lng; s.noRoad = false;
        s.label = stopLabelAt(ll.lat, ll.lng);
        saveTour(); stopGuidance();
        // auto mode re-optimizes around the moved point; a hand order only re-routes
        if (tourManual) applyManualOrder(); else optimize();
      });
      m.addTo(tourLayer);
    });
  }

  // Honest label for a hand-placed stop: whatever actually sits at that spot.
  function stopLabelAt(lat, lng) {
    var c = cellAt(lat, lng);
    if (c) {
      return c.status === 'enemy' ? (c.gang || T.unclaimed)
           : c.status === 'free' ? T.free_tag : T.gang_your;
    }
    var vk = Math.floor(lat / grid.lat + 1e-9) + '_' + Math.floor(lng / grid.lng + 1e-9);
    if (virginByKey[vk]) return T.virgin_land;
    return lat.toFixed(3) + ',' + lng.toFixed(3);
  }

  // Google Maps takes at most 9 waypoints + 1 destination = 10 stops. Everything
  // beyond that used to be cut off SILENTLY — now we say so.
  var MAPS_MAX = 10;
  function mapsUrl() {
    var stops = (tourOrdered || tour).slice(0, MAPS_MAX);
    if (!stops.length) return '#';
    // Road points instead of cell centers — otherwise Google sends you into a field
    var ll = stops.map(stopPos);
    var dest = ll[ll.length - 1], wps = ll.slice(0, -1);
    var u = 'https://www.google.com/maps/dir/?api=1&travelmode=driving&destination=' +
            dest.lat + ',' + dest.lng;
    if (wps.length) u += '&waypoints=' + wps.map(function (s) { return s.lat + ',' + s.lng; }).join('|');
    return u;
  }

  // ---- Short notice above the map, announced by the herald ----
  // The warlord leaps out of the map center, pauses menacingly for a moment and
  // flies, shrinking, into the notice, where he stays seated as a seal.
  var toastEl = document.getElementById('toast');
  var toastTxt = document.getElementById('toast-txt');
  var toastApe = document.getElementById('toast-ape');
  var toastT = null, heraldT = [];
  var REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- Jump to the scene of the action (click on a watcher report) ----
  // The map flies to the cell, the warlord strikes there and puffs away — like a
  // "HERE!" finger pointing at the crime scene.
  function apeStrike() {
    var stage = document.querySelector('.stage');
    if (!stage || REDUCED) return;
    var sr = stage.getBoundingClientRect();
    var SIZE = Math.min(180, Math.round(Math.min(sr.width, sr.height) * 0.5));
    var burst = document.createElement('div');
    burst.className = 'ape-burst strike';
    burst.innerHTML = '<span class="ape-ring"></span>' +
                      '<img src="/static/img/ape-herald.webp" alt="">';
    burst.style.width = burst.style.height = SIZE + 'px';
    burst.style.left = Math.round(sr.width / 2 - SIZE / 2) + 'px';
    burst.style.top = Math.round(sr.height / 2 - SIZE / 2) + 'px';
    stage.appendChild(burst);
    burst.classList.add('pop');
    setTimeout(function () {
      burst.classList.add('vanish');
      setTimeout(function () { if (burst.parentNode) burst.parentNode.removeChild(burst); }, 480);
    }, 950);
  }

  var strikePending = false;
  function jumpToEvent(lat, lng) {
    strikePending = true;
    function fire() { if (!strikePending) return; strikePending = false; apeStrike(); }
    map.once('moveend', fire);
    setTimeout(fire, 1100);   // fallback in case the map barely moves (moveend never fires)
    map.flyTo([lat, lng], Math.max(map.getZoom(), 13), {duration: 0.8});
  }

  document.addEventListener('click', function (e) {
    var ev = e.target.closest ? e.target.closest('.ev[data-lat]') : null;
    if (!ev) return;
    jumpToEvent(parseFloat(ev.dataset.lat), parseFloat(ev.dataset.lng));
  });

  function hideToast() {
    toastEl.classList.remove('on');
    setTimeout(function () { toastEl.hidden = true; }, 300);
  }

  function toast(html, ms, herald) {
    if (!toastEl) return;
    heraldT.forEach(clearTimeout); heraldT = [];
    if (toastT) clearTimeout(toastT);
    toastTxt.innerHTML = html;
    var life = ms || 5000;

    if (!herald || REDUCED || !document.querySelector('.stage')) {
      toastApe.classList.toggle('on', !!herald);
      toastEl.hidden = false;
      requestAnimationFrame(function () { toastEl.classList.add('on'); });
      toastT = setTimeout(hideToast, life);
      return;
    }

    // 1) Herald bursts out of the center — the notice is already there, just invisible,
    //    so we can measure the seal's target spot.
    toastApe.classList.remove('on');
    toastEl.hidden = false;
    toastEl.classList.remove('on');
    toastEl.classList.add('pre');

    var stage = document.querySelector('.stage');
    var burst = document.createElement('div');
    burst.className = 'ape-burst';
    burst.innerHTML = '<span class="ape-ring"></span>' +
                      '<img src="/static/img/ape-herald.webp" alt="">';
    stage.appendChild(burst);

    var sr = stage.getBoundingClientRect();
    var SIZE = Math.min(200, Math.round(Math.min(sr.width, sr.height) * 0.52));
    burst.style.width = burst.style.height = SIZE + 'px';
    burst.style.left = Math.round(sr.width / 2 - SIZE / 2) + 'px';
    burst.style.top = Math.round(sr.height * 0.44 - SIZE / 2) + 'px';
    burst.classList.add('pop');

    // 2) …pauses, then flies into the notice's seal slot.
    heraldT.push(setTimeout(function () {
      var br = burst.getBoundingClientRect();
      var tr = toastApe.getBoundingClientRect();
      var dx = (tr.left + tr.width / 2) - (br.left + br.width / 2);
      var dy = (tr.top + tr.height / 2) - (br.top + br.height / 2);
      var sc = tr.width / SIZE;
      burst.classList.remove('pop');
      burst.classList.add('fly');
      var anim = burst.animate([
        {transform: 'translate(0,0) scale(1) rotate(0deg)', opacity: 1},
        {transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + sc + ') rotate(-12deg)',
         opacity: 1}
      ], {duration: 480, easing: 'cubic-bezier(.55,-0.2,.35,1.2)', fill: 'forwards'});
      toastEl.classList.remove('pre');
      requestAnimationFrame(function () { toastEl.classList.add('on'); });
      anim.onfinish = function () {
        toastApe.classList.add('on');
        if (burst.parentNode) burst.parentNode.removeChild(burst);
      };
    }, 820));

    toastT = setTimeout(hideToast, life + 1300);
  }

  // Selected cells pulse on the map — immediately on tap, not only after
  // optimizing. Separate pane with one outline per stop: that way it also works
  // for virgin land (whose cell rectangles are not drawn while the layer is off).
  map.createPane('picks');
  map.getPane('picks').style.zIndex = 450;
  var pickLayer = L.layerGroup().addTo(map);
  function markTourCells() {
    pickLayer.clearLayers();
    if (!tour || !tour.length) return;
    tour.forEach(function (t) {
      var i = Math.floor(t.lat / grid.lat + 1e-9), j = Math.floor(t.lng / grid.lng + 1e-9);
      var r = L.rectangle([[i * grid.lat, j * grid.lng],
                           [(i + 1) * grid.lat, (j + 1) * grid.lng]],
        {pane: 'picks', color: '#ffd15e', weight: 2, fill: false, interactive: false});
      r.addTo(pickLayer);
      var el = r.getElement();
      if (el) el.classList.add('cell-picked');
    });
  }

  function renderTour() {
    var n = tour.length;
    panel.hidden = n === 0;
    // Persistent telemetry (step 10): the PLAN tab carries the stop count so an
    // active tour is visible from any tab / with the sheet collapsed.
    var tb = document.getElementById('tour-badge');
    if (tb) { tb.textContent = n; tb.hidden = n === 0; }
    document.querySelectorAll('.tour-add').forEach(function (b) {
      var on = inTour(b.dataset.lat, b.dataset.lng);
      b.classList.toggle('on', on); b.textContent = on ? '✓' : '+';
    });
    markTourCells();
    var warn = document.getElementById('tour-warn');
    if (warn) {
      // Persistent notice while the tour is over the Google limit
      warn.hidden = n <= MAPS_MAX;
      if (n > MAPS_MAX) {
        warn.innerHTML = '⚠ <b>' + esc(tf(T.maps_cap, {max: MAPS_MAX})) + '</b> — ' +
                         tf(T.maps_cap_note, {n: n, max: MAPS_MAX});
      }
    }
    if (!n) { tourLayer.clearLayers(); return; }
    var km = totalKm();
    // routed = real road km; fallback air-line totals are marked approximate
    var kmTxt = km != null ? (routeKm != null ? fmtDist(km) : '≈ ' + fmtDist(km)) : null;
    tourCount.textContent = kmTxt != null ? tf(T.tour_total, {n: n, d: kmTxt}) : tf(T.tour_one, {n: n});
    tourList.innerHTML = tour.map(function (s, i) {
      return '<li data-i="' + i + '"' + (tourOrdered && i >= MAPS_MAX ? ' class="over-cap"' : '') + '>' +
        '<span class="tour-drag" aria-hidden="true">≡</span>' +
        (tourOrdered ? '<b>' + (i + 1) + '.</b> ' : '') +
        esc(s.label || (s.lat.toFixed(3) + ',' + s.lng.toFixed(3))) +
        (s.noRoad ? ' <span class="no-road" title="' + esc(T.no_road) + '">⚑</span>' : '') +
        '<button type="button" class="tour-del" data-lat="' + s.lat + '" data-lng="' + s.lng + '">✕</button></li>';
    }).join('');
    var ab = document.getElementById('tour-auto');
    if (ab) ab.hidden = !tourManual;   // only offered while a hand order is active
    mapsLink.href = mapsUrl();
  }

  function stopGuidance() {
    guidanceOn = false; navBanner.hidden = true;
    var g = document.getElementById('tour-go'); if (g) g.textContent = T.tour_go;
    var h = document.getElementById('map-hero'); if (h) h.style.visibility = '';
    renderNextmove();   // the bottom slot is free again → the next-move card may return
    var mp = myPos(); if (mp) updateHere(mp.lat, mp.lng);   // restore the here-banner the strip replaced
  }
  function startGuidance() {
    if (!tourOrdered || !tourOrdered.length) { if (tourManual) applyManualOrder(); else optimize(); }
    if (!tourOrdered || !tourOrdered.length) return;
    guidanceOn = true; navIdx = 0;
    // always refetch on guidance start: an earlier fetch may have failed, been
    // skipped (snap pending), or lack the approach leg from the current position
    fetchRoute();
    here.hidden = true;   // slot spec: the nav strip owns the bottom during guidance
    renderNextmove();     // guidance takes the bottom slot → hide any next-move card
    var h = document.getElementById('map-hero'); if (h) h.style.visibility = 'hidden';
    document.getElementById('tour-go').textContent = T.tour_stop_nav;
    if (!watchId && navigator.geolocation) {
      watchId = navigator.geolocation.watchPosition(onPos, onPosErr, {enableHighAccuracy: true, maximumAge: 5000, timeout: 15000});
      follow = true;
    }
    var mp = myPos(); if (mp) navUpdate(mp.lat, mp.lng);
    else { navBanner.hidden = false; navBanner.className = 'nav-banner'; navBody.innerHTML = tf(T.nav_to, {k: 1, n: tourOrdered.length, d: '…'}); }
  }
  function navUpdate(lat, lng) {
    if (!guidanceOn || !tourOrdered) return;
    while (navIdx < tourOrdered.length &&
           cellKeyOf(lat, lng) === cellKeyOf(tourOrdered[navIdx].lat, tourOrdered[navIdx].lng)) navIdx++;
    if (navIdx >= tourOrdered.length) {
      navBanner.hidden = false; navBanner.className = 'nav-banner done';
      navBody.innerHTML = tf(T.nav_done, {n: tourOrdered.length});
      stopGuidance(); return;
    }
    // Arrow and distance point at the road point; the auto-advance above keeps
    // comparing cell keys (center) — that stays correct.
    var tgt = stopPos(tourOrdered[navIdx]), pt = {lat: lat, lng: lng};
    navBanner.hidden = false; navBanner.className = 'nav-banner';
    navBody.innerHTML = '<span class="nav-arrow" style="transform:rotate(' + bearing(pt, tgt).toFixed(0) + 'deg)">↑</span> ' +
      tf(T.nav_to, {k: navIdx + 1, n: tourOrdered.length, d: fmtDist(hav(pt, tgt))});
  }

  document.addEventListener('click', function (e) {
    var add = e.target.closest ? e.target.closest('.tour-add') : null;
    if (add) { e.preventDefault(); toggleTour(add.dataset.lat, add.dataset.lng, add.dataset.label); return; }
    var del = e.target.closest ? e.target.closest('.tour-del') : null;
    if (del) { e.preventDefault(); toggleTour(del.dataset.lat, del.dataset.lng); return; }
    var pin = e.target.closest ? e.target.closest('.cell-tour') : null;
    if (pin) { e.preventDefault(); toggleTour(pin.dataset.lat, pin.dataset.lng, pin.dataset.label); map.closePopup(); }
  });
  document.getElementById('tour-clear').addEventListener('click', function () {
    tour = []; tourOrdered = null; routeGeo = null; routeKm = null;
    setManual(false);
    stopGuidance(); tourLayer.clearLayers(); saveTour(); renderTour();
  });
  document.getElementById('tour-auto').addEventListener('click', function () {
    setManual(false);
    optimize();
  });

  // ---- Drag & drop reordering of tour stops ----
  // Pointer events, not HTML5 drag&drop: this must work with a thumb on the
  // phone. Dragging only starts on the ≡ handle so the list still scrolls
  // normally; the row follows the finger, siblings shift live, and the drop
  // commits the DOM order back into `tour` and switches to hand-order mode.
  (function () {
    var dragLi = null, dragPid = null;
    tourList.addEventListener('pointerdown', function (e) {
      var h = e.target.closest ? e.target.closest('.tour-drag') : null;
      if (!h) return;
      dragLi = h.closest('li');
      if (!dragLi) return;
      dragPid = e.pointerId;
      dragLi.classList.add('dragging');
      try { tourList.setPointerCapture(dragPid); } catch (ex) {}
      e.preventDefault();
    });
    tourList.addEventListener('pointermove', function (e) {
      if (!dragLi || e.pointerId !== dragPid) return;
      e.preventDefault();
      var lis = [].slice.call(tourList.children);
      for (var i = 0; i < lis.length; i++) {
        if (lis[i] === dragLi) continue;
        var r = lis[i].getBoundingClientRect();
        if (e.clientY < r.top + r.height / 2) {
          if (dragLi.nextSibling !== lis[i]) tourList.insertBefore(dragLi, lis[i]);
          return;
        }
      }
      if (tourList.lastElementChild !== dragLi) tourList.appendChild(dragLi);
    });
    function drop(e) {
      if (!dragLi || (e.pointerId !== undefined && e.pointerId !== dragPid)) return;
      dragLi.classList.remove('dragging');
      try { tourList.releasePointerCapture(dragPid); } catch (ex) {}
      var order = [].slice.call(tourList.children).map(function (li) {
        return tour[+li.dataset.i];
      }).filter(Boolean);
      dragLi = null; dragPid = null;
      if (order.length !== tour.length) { renderTour(); return; }   // defensive
      var changed = order.some(function (s, i) { return s !== tour[i]; });
      tour = order;
      saveTour();
      if (!changed) { renderTour(); return; }   // picked up and put back
      setManual(true);
      stopGuidance();   // same rule as add/remove: order changed under the tour
      applyManualOrder();
    }
    tourList.addEventListener('pointerup', drop);
    tourList.addEventListener('pointercancel', drop);
  })();
  document.getElementById('tour-go').addEventListener('click', function () {
    if (guidanceOn) stopGuidance(); else startGuidance();
  });
  // Skip the current stop without leaving the car: a closed bridge or private
  // lane must not brick the tour. The stop stays in the list — only guidance
  // moves past it (auto-advance still fires if the cell is entered later).
  document.getElementById('nav-skip').addEventListener('click', function () {
    if (!guidanceOn || !tourOrdered) return;
    navIdx++;
    var mp = myPos();
    if (mp) { navUpdate(mp.lat, mp.lng); return; }
    if (navIdx >= tourOrdered.length) {
      navBanner.hidden = false; navBanner.className = 'nav-banner done';
      navBody.innerHTML = tf(T.nav_done, {n: tourOrdered.length});
      stopGuidance(); return;
    }
    navBody.innerHTML = tf(T.nav_to, {k: navIdx + 1, n: tourOrdered.length, d: '…'});
  });
  renderTour();
  // A restored fully-snapped tour never reaches doSnap's optimize path — order
  // it right away so the Maps link is never insertion-order. A restored HAND
  // order is authoritative as saved: route it, don't re-sort it.
  if (tour.length) { if (tourManual) applyManualOrder(); else optimize(); }
  snapTour();   // retroactively snap a tour saved in an old session
  plRefresh();

  // ---- Raven post: web push on/off for this device ----
  (function () {
    var box = document.getElementById('push-box');
    if (!box || !('serviceWorker' in navigator) || !('PushManager' in window) ||
        !('Notification' in window)) return;
    box.hidden = false;
    var stat = document.getElementById('push-status');
    var btn = document.getElementById('push-toggle');
    var sub = null;
    function ui() {
      stat.textContent = sub ? T.push_on : T.push_off;
      btn.textContent = sub ? T.push_disable : T.push_enable;
      box.classList.toggle('on', !!sub);
    }
    navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription();
    }).then(function (s) { sub = s; ui(); }).catch(function () {});
    function b64ToU8(s) {
      var pad = '='.repeat((4 - s.length % 4) % 4);
      var raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
      var arr = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
      return arr;
    }
    btn.addEventListener('click', function () {
      if (sub) {
        var ep = sub.endpoint;
        sub.unsubscribe().catch(function () {});
        fetch('/push/unsubscribe', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({endpoint: ep})}).catch(function () {});
        sub = null; ui();
        return;
      }
      btn.disabled = true;
      stat.textContent = T.push_working;
      // Never hang silently again: every stage gets a deadline, otherwise the UI says why.
      function withTimeout(p, ms, what) {
        return Promise.race([p, new Promise(function (_, rej) {
          setTimeout(function () { rej(new Error(what)); }, ms);
        })]);
      }
      Notification.requestPermission().then(function (perm) {
        if (perm !== 'granted') { btn.disabled = false; stat.textContent = T.push_denied; return; }
        return Promise.all([withTimeout(navigator.serviceWorker.ready, 8000, 'service worker'),
                            fetch('/push/pubkey').then(function (r) { return r.json(); })])
          .then(function (rs) {
            // First pick up a possibly orphaned subscription (server no longer knows it)
            return withTimeout(rs[0].pushManager.getSubscription().then(function (old) {
              if (old) return old;
              return rs[0].pushManager.subscribe({userVisibleOnly: true,
                applicationServerKey: b64ToU8(rs[1].key)});
            }), 15000, 'subscribe');
          })
          .then(function (s) {
            sub = s;
            return fetch('/push/subscribe', {method: 'POST',
              headers: {'Content-Type': 'application/json'}, body: JSON.stringify(s.toJSON())})
              .then(function (r) {
                if (!r.ok) throw new Error('subscribe ' + r.status);
                btn.disabled = false; ui();
              });
          })
          .catch(function (err) {
            btn.disabled = false; sub = null; ui();
            stat.textContent = (err && err.message) ? (T.push_failed + ' (' + err.message + ')')
                                                    : T.push_failed;
          });
      }).catch(function () { btn.disabled = false; stat.textContent = T.push_denied; });
    });
  })();

  // Mobile: Leaflet often initializes before the flex layout has the real map
  // size → the map stays empty/wrong until you reload manually. invalidateSize +
  // refit once the layout has settled (several times, up to window.load).
  // On first build the map is still on the start view (Atlantic) — the distances
  // therefore have to be pulled along once it is fitted to the turf.
  function relayout() { map.invalidateSize(); fitInitial(); plRefresh(); }
  setTimeout(relayout, 250);
  setTimeout(relayout, 900);
  window.addEventListener('load', relayout);
  window.addEventListener('resize', function () { map.invalidateSize(); });
});
