/* Trends page: chart readouts.
 *
 * The charts are server-rendered SVG and were, in the owner's words, "just
 * pictures". This makes every shape say what it is.
 *
 * The mechanism is deliberately small: the server already knows the geometry, so
 * it emits an invisible hit area per data point carrying a finished sentence in
 * data-tip. This file only decides which one is under the pointer and writes it
 * into that chart's readout line. No nearest-point maths that would have to
 * re-derive the layout, no measurements, nothing to keep in sync.
 *
 * Pointer events, not mouse events: the same code path then serves a finger.
 * On touch the readout stays until the next tap somewhere else, because a
 * tooltip that vanishes with the finger that summoned it is unreadable.
 */
(function () {
  'use strict';

  var charts = document.querySelectorAll('[data-readout]');
  if (!charts.length) return;

  function readoutOf(box) { return box.querySelector('.an-readout'); }

  function show(box, el) {
    var out = readoutOf(box);
    if (!out) return;
    var tip = el && el.getAttribute('data-tip');
    if (!tip) return;
    out.textContent = tip;
    out.classList.add('live');
    var prev = box.querySelector('.an-hit.on');
    if (prev) prev.classList.remove('on');
    el.classList.add('on');
  }

  function reset(box) {
    var out = readoutOf(box);
    if (!out) return;
    // The default is the source tag — where the number came from. Restoring it
    // rather than blanking keeps the honesty line visible whenever nothing is
    // being pointed at.
    out.textContent = out.getAttribute('data-default') || '';
    out.classList.remove('live');
    var on = box.querySelector('.an-hit.on');
    if (on) on.classList.remove('on');
  }

  Array.prototype.forEach.call(charts, function (box) {
    var out = readoutOf(box);
    if (out && !out.hasAttribute('data-default')) {
      out.setAttribute('data-default', out.textContent.trim());
    }

    box.addEventListener('pointermove', function (e) {
      var hit = e.target.closest && e.target.closest('[data-tip]');
      if (hit) show(box, hit);
    });

    box.addEventListener('pointerdown', function (e) {
      var hit = e.target.closest && e.target.closest('[data-tip]');
      if (hit) show(box, hit);
    });

    // A mouse leaving restores the default; a finger lifting does not, so the
    // reading survives long enough to be read.
    box.addEventListener('pointerleave', function (e) {
      if (e.pointerType !== 'touch') reset(box);
    });

    // Keyboard: the hit areas are focusable, so tabbing through a chart reads
    // it out point by point.
    box.addEventListener('focusin', function (e) {
      var hit = e.target.closest && e.target.closest('[data-tip]');
      if (hit) show(box, hit);
    });
    box.addEventListener('focusout', function () { reset(box); });
  });

  // A tap anywhere else clears whichever chart is holding a touch reading.
  document.addEventListener('pointerdown', function (e) {
    Array.prototype.forEach.call(charts, function (box) {
      if (!box.contains(e.target)) reset(box);
    });
  });
}());
