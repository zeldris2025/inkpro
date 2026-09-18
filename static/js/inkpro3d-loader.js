/* Capability gate and lazy loader for the WebGL layer.
 *
 * This file is tiny and synchronous-safe; the ~600KB Three.js bundle behind it
 * is only fetched once the page has painted, the browser is idle, and the
 * device has been judged capable. The quote-builder CTA in the hero is never
 * waiting on it.
 */
(function () {
  'use strict';

  var layer = document.getElementById('ink3d-layer');
  if (!layer) return;

  /** Reasons to leave the static fallback in place. */
  function decline() {
    // Respect the OS-level motion preference above everything else.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return 'reduced-motion';

    // Data Saver: fetching a large 3D bundle is exactly what it asks us not to do.
    var connection = navigator.connection || {};
    if (connection.saveData) return 'save-data';
    if (/(^|-)2g$/.test(connection.effectiveType || '')) return 'slow-network';

    if (navigator.deviceMemory && navigator.deviceMemory < 4) return 'low-memory';
    if (navigator.hardwareConcurrency && navigator.hardwareConcurrency < 4) return 'few-cores';

    // Probe for a real WebGL context rather than trusting feature flags.
    try {
      var probe = document.createElement('canvas');
      var gl = probe.getContext('webgl2') || probe.getContext('webgl');
      if (!gl) return 'no-webgl';
      // Software rasterisers (SwiftShader, llvmpipe) report as WebGL but crawl.
      var info = gl.getExtension('WEBGL_debug_renderer_info');
      if (info) {
        var renderer = String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL) || '');
        if (/swiftshader|llvmpipe|software/i.test(renderer)) return 'software-renderer';
      }
    } catch (error) {
      return 'webgl-probe-failed';
    }
    return null;
  }

  /** Phones get the scene, but at a reduced budget. */
  function quality() {
    var coarse = window.matchMedia('(pointer: coarse)').matches;
    var small = Math.min(window.innerWidth, window.innerHeight) < 820;
    var modest = (navigator.deviceMemory || 8) < 8 || (navigator.hardwareConcurrency || 8) < 8;
    return (coarse && small) || modest ? 'low' : 'high';
  }

  var reason = decline();
  if (reason) {
    layer.setAttribute('data-3d', 'skipped:' + reason);
    return; // The static fallback illustration stays exactly as it is.
  }

  function boot() {
    import('' + layer.dataset.moduleUrl)
      .then(function (module) {
        module.init({
          canvas: document.getElementById('ink3d-canvas'),
          layer: layer,
          logoUrl: layer.dataset.logoUrl,
          quality: quality(),
        });
        layer.setAttribute('data-3d', 'active');
      })
      .catch(function (error) {
        // A CDN hiccup must never cost the visitor the page.
        layer.setAttribute('data-3d', 'failed');
        if (window.console) console.warn('InkPro 3D layer unavailable:', error);
      });
  }

  // Wait for paint, then for the browser to be idle.
  function schedule() {
    if ('requestIdleCallback' in window) {
      requestIdleCallback(boot, { timeout: 2500 });
    } else {
      setTimeout(boot, 600);
    }
  }

  if (document.readyState === 'complete') schedule();
  else window.addEventListener('load', schedule);
})();
