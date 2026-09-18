/* InkPro front-end behaviour: scroll reveals, counters and micro-interactions.
 *
 * Everything degrades safely. If GSAP fails to load from the CDN, or the
 * visitor prefers reduced motion, the reveal classes are cleared immediately
 * so no content is left invisible.
 */
(function () {
  'use strict';

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function showEverything() {
    document.querySelectorAll('.reveal').forEach(function (el) {
      el.style.opacity = 1;
      el.style.transform = 'none';
    });
  }

  function initReveals() {
    if (typeof gsap === 'undefined' || typeof ScrollTrigger === 'undefined' || reduced) {
      showEverything();
      return;
    }
    gsap.registerPlugin(ScrollTrigger);
    document.documentElement.classList.add('reveal-ready');

    // Group reveals by their nearest [data-reveal-group] so cards stagger in
    // together rather than one long chain down the page.
    document.querySelectorAll('[data-reveal-group]').forEach(function (group) {
      var items = group.querySelectorAll('.reveal');
      if (!items.length) return;
      gsap.to(items, {
        opacity: 1,
        y: 0,
        duration: 0.7,
        ease: 'power3.out',
        stagger: 0.08,
        scrollTrigger: { trigger: group, start: 'top 82%', once: true },
      });
    });

    // Standalone reveals not inside a group.
    document.querySelectorAll('.reveal').forEach(function (el) {
      if (el.closest('[data-reveal-group]')) return;
      gsap.to(el, {
        opacity: 1,
        y: 0,
        duration: 0.7,
        ease: 'power3.out',
        scrollTrigger: { trigger: el, start: 'top 88%', once: true },
      });
    });

    // Gentle parallax on anything tagged for it.
    document.querySelectorAll('[data-parallax]').forEach(function (el) {
      var depth = parseFloat(el.dataset.parallax) || 0.2;
      gsap.to(el, {
        yPercent: depth * -18,
        ease: 'none',
        scrollTrigger: { trigger: el, start: 'top bottom', end: 'bottom top', scrub: true },
      });
    });
  }

  /* Animated stat counters. */
  function initCounters() {
    var counters = document.querySelectorAll('[data-count-to]');
    if (!counters.length) return;

    function run(el) {
      var target = parseFloat(el.dataset.countTo) || 0;
      var suffix = el.dataset.countSuffix || '';
      if (reduced) {
        el.textContent = target.toLocaleString() + suffix;
        return;
      }
      var start = performance.now();
      var duration = 1600;
      function tick(now) {
        var progress = Math.min((now - start) / duration, 1);
        // easeOutExpo, so the number lands rather than crawls.
        var eased = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress);
        el.textContent = Math.round(target * eased).toLocaleString() + suffix;
        if (progress < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    }

    if (!('IntersectionObserver' in window)) {
      counters.forEach(run);
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          run(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.4 });
    counters.forEach(function (el) { observer.observe(el); });
  }

  /* Ink splatter on primary button clicks. */
  function initSplatter() {
    if (reduced) return;
    document.addEventListener('click', function (event) {
      var trigger = event.target.closest('.ink-btn');
      if (!trigger) return;
      var splat = document.createElement('span');
      var size = 28;
      splat.className = 'ink-splat';
      splat.style.width = splat.style.height = size + 'px';
      splat.style.left = (event.clientX - size / 2) + 'px';
      splat.style.top = (event.clientY - size / 2) + 'px';
      document.body.appendChild(splat);
      setTimeout(function () { splat.remove(); }, 600);
    });
  }

  /* Hero entrance — the press "stamping" the wordmark down. */
  function initHero() {
    var hero = document.querySelector('[data-hero]');
    if (!hero || typeof gsap === 'undefined' || reduced) return;
    var timeline = gsap.timeline({ defaults: { ease: 'power4.out' } });
    timeline
      .from(hero.querySelectorAll('[data-hero-line]'), {
        yPercent: 110, opacity: 0, duration: 0.9, stagger: 0.1,
      })
      .from(hero.querySelectorAll('[data-hero-fade]'), {
        y: 20, opacity: 0, duration: 0.7, stagger: 0.08,
      }, '-=0.5')
      .from(hero.querySelectorAll('[data-hero-blob]'), {
        scale: 0.4, opacity: 0, duration: 1.2, stagger: 0.12, ease: 'elastic.out(1, 0.6)',
      }, '-=0.8');
  }

  function init() {
    initReveals();
    initCounters();
    initSplatter();
    initHero();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Re-run reveals for content HTMX swaps in after the initial load.
  document.body && document.body.addEventListener('htmx:afterSwap', function () {
    if (typeof ScrollTrigger !== 'undefined') ScrollTrigger.refresh();
  });
})();
