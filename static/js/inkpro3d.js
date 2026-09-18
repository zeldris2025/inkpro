/* InkPro — scroll-driven WebGL layer.
 *
 * An ES module, dynamically imported by `inkpro3d-loader.js` only after the
 * page has painted and only on devices that can afford it. Nothing here runs
 * on the critical path.
 *
 * Structure: one persistent scene holds five stage groups. Each stage is bound
 * to a `[data-scene]` section by its own ScrollTrigger with `scrub: true`, so
 * scroll position drives a normalised 0–1 progress that in turn drives
 * rotation, camera, material and particle state. Only the active stage's group
 * is visible and only its update function runs.
 *
 * All geometry is procedural — no model or texture downloads beyond the logo —
 * which keeps the payload small and the polycount in the low thousands, since
 * most quote requests arrive from phones.
 */

import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js';

const YELLOW = 0xffd200;
const BLACK = 0x0a0a0a;
const CYAN = 0x00aeef;
const MAGENTA = 0xec008c;
const WHITE = 0xffffff;

const lerp = (a, b, t) => a + (b - a) * t;
const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
/** Smooth 0→1→0 pulse, for stages that build then settle. */
const arch = (p) => Math.sin(clamp01(p) * Math.PI);
/** Ease the hard edges off a linear scrub. */
const easeInOut = (p) => (p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2);

export function init(options) {
  const { canvas, layer, logoUrl, quality = 'high' } = options;
  const gsap = window.gsap;
  const ScrollTrigger = window.ScrollTrigger;
  if (!gsap || !ScrollTrigger) {
    throw new Error('GSAP with ScrollTrigger must be loaded before the 3D layer.');
  }
  gsap.registerPlugin(ScrollTrigger);

  const low = quality === 'low';

  // --- renderer ------------------------------------------------------------
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: !low,
    alpha: true,
    powerPreference: low ? 'low-power' : 'high-performance',
  });
  renderer.setClearAlpha(0);
  // Capping DPR matters more than any geometry decision on a phone: an
  // uncapped 3x retina buffer costs ~9x the fragment work of a 1x one.
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, low ? 1.25 : 1.75));
  renderer.setSize(window.innerWidth, window.innerHeight, false);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, window.innerWidth / window.innerHeight, 0.1, 100);
  camera.position.set(0, 0, 16);

  // Every stage group hangs off this root. The scene is scenery behind the
  // copy, not the subject of the page, so it is scaled down and pushed off
  // centre rather than sitting square behind the headline.
  const root = new THREE.Group();
  root.scale.setScalar(1.05);
  scene.add(root);

  // --- lighting ------------------------------------------------------------
  // One directional key plus a hemisphere fill: enough for believable PBR
  // shading without the cost of shadow maps or an environment probe.
  const key = new THREE.DirectionalLight(WHITE, 2.6);
  key.position.set(4, 6, 6);
  scene.add(key);
  scene.add(new THREE.HemisphereLight(WHITE, BLACK, 0.55));
  const brandAccent = new THREE.PointLight(YELLOW, 18, 24);
  brandAccent.position.set(-3, 2, 4);
  scene.add(brandAccent);

  // --- shared materials ----------------------------------------------------
  const materials = {
    metal: new THREE.MeshStandardMaterial({
      color: 0x6b6b76, metalness: 0.9, roughness: 0.28,
      emissive: 0x1a1a1f, emissiveIntensity: 1,
    }),
    yellow: new THREE.MeshStandardMaterial({
      color: YELLOW, metalness: 0.4, roughness: 0.28,
      emissive: YELLOW, emissiveIntensity: 0.28,
    }),
    rubber: new THREE.MeshStandardMaterial({
      color: 0x18181b, metalness: 0.2, roughness: 0.7,
      emissive: 0x0f0f12, emissiveIntensity: 1,
    }),
    paper: new THREE.MeshStandardMaterial({
      color: 0xf5f5f5, metalness: 0.0, roughness: 0.85, side: THREE.DoubleSide,
      emissive: 0x2a2a2a, emissiveIntensity: 1,
    }),
  };

  const segments = low ? 12 : 24;
  const stages = {};

  // =========================================================================
  // Stage 1 — the press. Idle at rest, rollers spin up on first scroll and
  // ink begins to drip.
  // =========================================================================
  const press = new THREE.Group();
  {
    const frame = new THREE.Group();
    const upright = new THREE.BoxGeometry(0.4, 4, 0.4);
    [-2.6, 2.6].forEach((x) => {
      const post = new THREE.Mesh(upright, materials.yellow);
      post.position.set(x, 0, 0);
      frame.add(post);
    });
    const crossbar = new THREE.Mesh(new THREE.BoxGeometry(5.6, 0.32, 0.32), materials.yellow);
    crossbar.position.y = 1.9;
    frame.add(crossbar);
    const bed = new THREE.Mesh(new THREE.BoxGeometry(5.6, 0.22, 1.6), materials.yellow);
    bed.position.y = -1.9;
    frame.add(bed);
    press.add(frame);

    // Counter-rotating rollers.
    const rollerGeo = new THREE.CylinderGeometry(0.55, 0.55, 5.0, segments);
    press.userData.rollers = [0.75, -0.75].map((y, index) => {
      const roller = new THREE.Mesh(rollerGeo, index === 0 ? materials.rubber : materials.yellow);
      roller.rotation.z = Math.PI / 2;
      roller.position.y = y;
      press.add(roller);
      return roller;
    });

    press.add(makeInkDrips(press, low ? 90 : 220));
  }
  root.add(press);
  stages.press = {
    group: press,
    camera: { from: [0, 0.8, 15.5], to: [0, -0.4, 12.5] },
    update(p) {
      const spin = easeInOut(p);
      press.userData.rollers[0].rotation.y += 0.02 + spin * 0.22;
      press.userData.rollers[1].rotation.y -= 0.02 + spin * 0.22;
      press.rotation.y = lerp(-0.35, 0.18, p);
      press.userData.drips.material.opacity = spin * 0.9;
      press.userData.dripSpeed = 0.01 + spin * 0.05;
    },
  };

  // =========================================================================
  // Stage 2 — a printed sheet peels off the bed and rotates to reveal the
  // product. Three low-poly product meshes cross-fade in sequence rather than
  // true vertex morphing: identical visual read, a fraction of the cost, and
  // it lets each product keep its own silhouette.
  // =========================================================================
  const sheetStage = new THREE.Group();
  {
    const sheet = new THREE.Mesh(new THREE.PlaneGeometry(3.4, 2.4, 1, 1), materials.paper);
    sheetStage.add(sheet);
    sheetStage.userData.sheet = sheet;
    sheetStage.userData.products = [makeShirt(), makeBanner(), makeSticker()];
    sheetStage.userData.products.forEach((mesh) => {
      mesh.visible = false;
      sheetStage.add(mesh);
    });
  }
  sheetStage.visible = false;
  root.add(sheetStage);
  stages.sheet = {
    group: sheetStage,
    camera: { from: [0, 0.2, 13], to: [0, -0.2, 10.5] },
    update(p) {
      const { sheet, products } = sheetStage.userData;
      // First fifth: the sheet peels off the bed and flips away.
      const peel = clamp01(p / 0.2);
      sheet.rotation.x = lerp(-Math.PI / 2.1, 0, easeInOut(peel));
      sheet.position.y = lerp(-1.6, 0, easeInOut(peel));
      sheet.material.opacity = 1 - clamp01((p - 0.16) / 0.1);
      sheet.material.transparent = true;
      sheet.visible = sheet.material.opacity > 0.01;

      // Remaining four fifths: cycle through the products.
      const cycle = clamp01((p - 0.2) / 0.8) * products.length;
      products.forEach((mesh, index) => {
        const distance = Math.abs(cycle - (index + 0.5));
        const presence = clamp01(1 - distance);
        mesh.visible = presence > 0.01;
        if (!mesh.visible) return;
        mesh.scale.setScalar(0.72 + presence * 0.34);
        mesh.rotation.y = p * Math.PI * 2.2 + index * 0.6;
        mesh.rotation.z = Math.sin(p * Math.PI * 2 + index) * 0.08;
        mesh.traverse((child) => {
          if (!child.material) return;
          child.material.transparent = true;
          child.material.opacity = presence;
        });
      });
    },
  };

  // =========================================================================
  // Stage 3 — the CMYK separation. Four ink planes pull apart to show the
  // process, then recombine into the finished print.
  // =========================================================================
  const cmyk = new THREE.Group();
  {
    const planeGeo = new THREE.PlaneGeometry(3.2, 2.2);
    cmyk.userData.planes = [CYAN, MAGENTA, YELLOW, BLACK].map((colour, index) => {
      const plane = new THREE.Mesh(
        planeGeo,
        new THREE.MeshStandardMaterial({
          color: colour,
          transparent: true,
          opacity: 0.82,
          roughness: 0.45,
          metalness: 0.15,
          side: THREE.DoubleSide,
        })
      );
      plane.userData.index = index;
      cmyk.add(plane);
      return plane;
    });
  }
  cmyk.visible = false;
  root.add(cmyk);
  stages.cmyk = {
    group: cmyk,
    camera: { from: [0, 0, 8], to: [0, 0, 8] },
    update(p) {
      // Separate through the middle of the section, recombine by the end.
      const spread = arch(p);
      cmyk.userData.planes.forEach((plane, index) => {
        const offset = index - 1.5;
        plane.position.z = offset * spread * 1.5;
        plane.position.x = offset * spread * 0.55;
        plane.position.y = -offset * spread * 0.32;
        plane.material.opacity = lerp(0.95, 0.72, spread);
      });
      // Camera orbits the stack while they are apart.
      const angle = p * Math.PI * 1.1 - Math.PI * 0.55;
      const radius = lerp(11.5, 14.5, spread);
      camera.position.set(Math.sin(angle) * radius, spread * 2.0, Math.cos(angle) * radius);
      camera.lookAt(0, 0, 0);
      cmyk.userData.orbiting = true;
    },
  };

  // =========================================================================
  // Stage 4 — a printed banner floats and flips, catching the key light.
  // This is the stage that sells the material quality, so it carries the
  // highest metalness and the tightest roughness in the scene.
  // =========================================================================
  const showcase = new THREE.Group();
  {
    const card = new THREE.Mesh(
      new THREE.BoxGeometry(3.6, 2.0, 0.06),
      new THREE.MeshStandardMaterial({ color: YELLOW, metalness: 0.65, roughness: 0.22 })
    );
    const back = new THREE.Mesh(
      new THREE.BoxGeometry(3.4, 1.8, 0.08),
      new THREE.MeshStandardMaterial({ color: BLACK, metalness: 0.4, roughness: 0.5 })
    );
    back.position.z = -0.04;
    showcase.add(card, back);
    showcase.userData.card = card;
  }
  showcase.visible = false;
  root.add(showcase);
  stages.showcase = {
    group: showcase,
    camera: { from: [0, 0.3, 13.5], to: [0, -0.3, 11] },
    update(p) {
      showcase.rotation.y = p * Math.PI * 2.4 - Math.PI * 0.3;
      showcase.rotation.x = Math.sin(p * Math.PI * 2) * 0.22;
      showcase.position.y = Math.sin(p * Math.PI) * 0.5 - 0.2;
      // Sweep the key light across so the flip catches a highlight.
      key.position.set(lerp(-5, 5, p), 5, 6);
    },
  };

  // =========================================================================
  // Stage 5 — the press stamps the logo, ink bursts, everything settles.
  // =========================================================================
  const stamp = new THREE.Group();
  {
    const block = new THREE.Mesh(new THREE.BoxGeometry(3.0, 3.0, 0.7), materials.metal);
    block.position.y = 2.2;
    stamp.add(block);
    stamp.userData.block = block;

    const plate = new THREE.Mesh(
      new THREE.PlaneGeometry(3.6, 2.1),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, color: WHITE })
    );
    plate.position.y = -0.6;
    stamp.add(plate);
    stamp.userData.plate = plate;

    // The logo is the one texture the scene downloads, and only at this stage.
    if (logoUrl) {
      new THREE.TextureLoader().load(logoUrl, (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        plate.material.map = texture;
        plate.material.needsUpdate = true;
      });
    }
    stamp.add(makeSplatter(stamp, low ? 120 : 300));
  }
  stamp.visible = false;
  root.add(stamp);
  stages.stamp = {
    group: stamp,
    camera: { from: [0, 0.8, 14], to: [0, 0.2, 11.5] },
    update(p) {
      // Drop, impact at 45%, then settle.
      const impact = 0.45;
      const block = stamp.userData.block;
      if (p < impact) {
        block.position.y = lerp(3.4, 0.36, easeInOut(p / impact));
      } else {
        const rebound = (p - impact) / (1 - impact);
        block.position.y = 0.36 + Math.sin(rebound * Math.PI) * 0.8 + rebound * 2.4;
      }
      stamp.userData.plate.material.opacity = clamp01((p - impact) / 0.18);
      stamp.userData.burst = clamp01((p - impact) / 0.35);
    },
  };

  // =========================================================================
  // Stage binding — one ScrollTrigger per [data-scene] section.
  // =========================================================================
  let active = null;
  let activeProgress = 0;
  const triggers = [];

  const allStages = Object.values(stages);
  allStages.forEach((stage) => {
    stage.group.visible = false;
  });

  /** Exactly one stage is ever on screen; the rest are hidden and skipped.
   *
   * `force` re-applies visibility even when the stage has not changed. It is
   * needed for the initial seed because ScrollTrigger fires `onUpdate` during
   * `create()`, which sets `active` before any group has been shown. */
  function activate(stage, force) {
    if (active === stage && !force) return;
    active = stage;
    allStages.forEach((candidate) => {
      candidate.group.visible = candidate === stage;
    });
  }

  document.querySelectorAll('[data-scene]').forEach((section) => {
    const name = section.dataset.scene;
    const stage = stages[name];
    if (!stage) return;
    triggers.push(
      ScrollTrigger.create({
        trigger: section,
        // Centre-to-centre means adjacent sections hand over cleanly at the
        // viewport midline instead of both claiming the scene for the length
        // of their overlap.
        start: 'top center',
        end: 'bottom center',
        // Frame-accurate sync with the scrollbar, with a touch of smoothing so
        // a flung trackpad does not read as a jump cut.
        scrub: low ? true : 0.4,
        onUpdate(self) {
          activate(stage);
          activeProgress = self.progress;
        },
      })
    );
  });

  // Make sure something is on screen for a visitor who lands on the page and
  // reads the hero before scrolling at all.
  activate(active || stages.press, true);
  ScrollTrigger.refresh();

  // =========================================================================
  // Render loop
  // =========================================================================
  const clock = new THREE.Clock();
  let running = true;
  let frame = 0;

  function resize() {
    const width = window.innerWidth;
    const height = window.innerHeight;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
  }

  function render() {
    if (!running) return;
    frame = requestAnimationFrame(render);
    const delta = Math.min(clock.getDelta(), 0.05);

    if (active) {
      // Stages that do not drive the camera themselves get the default dolly.
      const orbiting = active === stages.cmyk;
      active.update(activeProgress, delta);
      if (!orbiting) {
        const { from, to } = active.camera;
        const t = easeInOut(activeProgress);
        camera.position.set(lerp(from[0], to[0], t), lerp(from[1], to[1], t), lerp(from[2], to[2], t));
        camera.lookAt(0, 0, 0);
      }
    }

    updateInkDrips(press, delta);
    updateSplatter(stamp, delta);
    renderer.render(scene, camera);
  }

  function start() {
    if (running) return;
    running = true;
    clock.getDelta();
    render();
  }
  function stop() {
    running = false;
    cancelAnimationFrame(frame);
  }

  // Do not burn a phone's battery rendering a scene nobody is looking at.
  document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
  window.addEventListener('resize', resize);
  resize();
  render();

  // Reveal once there is something to see.
  requestAnimationFrame(() => {
    canvas.classList.remove('opacity-0');
    canvas.classList.add('ink3d-visible');
    const fallback = layer.querySelector('#ink3d-fallback');
    if (fallback) fallback.remove();
    document.documentElement.classList.add('has-3d');
  });

  return {
    // Exposed so the scene can be inspected from tests and the browser console.
    scene,
    camera,
    renderer,
    stages,
    getActive: () => ({ stage: active, progress: activeProgress }),
    destroy() {
      stop();
      triggers.forEach((trigger) => trigger.kill());
      window.removeEventListener('resize', resize);
      renderer.dispose();
      scene.traverse((object) => {
        if (object.geometry) object.geometry.dispose();
        if (object.material) {
          (Array.isArray(object.material) ? object.material : [object.material]).forEach((m) => m.dispose());
        }
      });
      document.documentElement.classList.remove('has-3d');
    },
  };

  // =========================================================================
  // Geometry helpers — deliberately crude shapes. At this camera distance and
  // opacity they read as products, and each costs a few dozen triangles.
  // =========================================================================
  function makeShirt() {
    const group = new THREE.Group();
    const body = new THREE.Mesh(
      new THREE.BoxGeometry(1.8, 2.2, 0.16),
      new THREE.MeshStandardMaterial({ color: BLACK, roughness: 0.9, metalness: 0.05 })
    );
    const sleeveGeo = new THREE.BoxGeometry(0.7, 0.7, 0.16);
    [-1.15, 1.15].forEach((x) => {
      const sleeve = new THREE.Mesh(sleeveGeo, body.material);
      sleeve.position.set(x, 0.72, 0);
      group.add(sleeve);
    });
    const print = new THREE.Mesh(new THREE.PlaneGeometry(1.0, 1.0), materials.yellow);
    print.position.z = 0.1;
    group.add(body, print);
    return group;
  }

  function makeBanner() {
    const group = new THREE.Group();
    const cloth = new THREE.Mesh(
      new THREE.PlaneGeometry(3.2, 1.5, low ? 6 : 14, 1),
      new THREE.MeshStandardMaterial({ color: YELLOW, roughness: 0.55, metalness: 0.2, side: THREE.DoubleSide })
    );
    // A gentle wave baked into the vertices reads as hanging fabric.
    const position = cloth.geometry.attributes.position;
    for (let i = 0; i < position.count; i += 1) {
      position.setZ(i, Math.sin(position.getX(i) * 1.6) * 0.16);
    }
    position.needsUpdate = true;
    cloth.geometry.computeVertexNormals();
    const poleGeo = new THREE.CylinderGeometry(0.06, 0.06, 1.9, 8);
    [-1.6, 1.6].forEach((x) => {
      const pole = new THREE.Mesh(poleGeo, materials.metal);
      pole.position.x = x;
      group.add(pole);
    });
    group.add(cloth);
    return group;
  }

  function makeSticker() {
    const group = new THREE.Group();
    const disc = new THREE.Mesh(
      new THREE.CylinderGeometry(1.1, 1.1, 0.05, low ? 16 : 40),
      new THREE.MeshStandardMaterial({ color: WHITE, roughness: 0.25, metalness: 0.1 })
    );
    disc.rotation.x = Math.PI / 2;
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(0.72, 0.16, 8, low ? 16 : 36),
      materials.yellow
    );
    ring.position.z = 0.06;
    group.add(disc, ring);
    return group;
  }

  // --- particles ----------------------------------------------------------
  function makeInkDrips(owner, count) {
    const positions = new Float32Array(count * 3);
    const speeds = new Float32Array(count);
    for (let i = 0; i < count; i += 1) {
      positions[i * 3] = (Math.random() - 0.5) * 5.0;
      positions[i * 3 + 1] = Math.random() * 2.2 - 0.4;
      positions[i * 3 + 2] = (Math.random() - 0.5) * 0.9;
      speeds[i] = 0.4 + Math.random() * 1.4;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const points = new THREE.Points(
      geometry,
      new THREE.PointsMaterial({ color: YELLOW, size: 0.07, transparent: true, opacity: 0, depthWrite: false })
    );
    owner.userData.drips = points;
    owner.userData.dripSpeeds = speeds;
    owner.userData.dripSpeed = 0;
    return points;
  }

  function updateInkDrips(owner, delta) {
    if (!owner.visible || !owner.userData.drips) return;
    const array = owner.userData.drips.geometry.attributes.position.array;
    const speeds = owner.userData.dripSpeeds;
    const rate = owner.userData.dripSpeed || 0;
    if (rate <= 0) return;
    for (let i = 0; i < speeds.length; i += 1) {
      array[i * 3 + 1] -= speeds[i] * rate * delta * 60;
      if (array[i * 3 + 1] < -2.2) array[i * 3 + 1] = 1.8 + Math.random() * 0.6;
    }
    owner.userData.drips.geometry.attributes.position.needsUpdate = true;
  }

  function makeSplatter(owner, count) {
    const positions = new Float32Array(count * 3);
    const directions = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      // Bias the burst outward and slightly up, like ink off a struck plate.
      const angle = Math.random() * Math.PI * 2;
      const lift = Math.random() * 0.8 + 0.1;
      directions[i * 3] = Math.cos(angle) * (0.6 + Math.random());
      directions[i * 3 + 1] = lift;
      directions[i * 3 + 2] = Math.sin(angle) * (0.6 + Math.random());
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const points = new THREE.Points(
      geometry,
      new THREE.PointsMaterial({ color: YELLOW, size: 0.09, transparent: true, opacity: 0, depthWrite: false })
    );
    owner.userData.splatter = points;
    owner.userData.splatterDirs = directions;
    owner.userData.burst = 0;
    return points;
  }

  function updateSplatter(owner) {
    if (!owner.visible || !owner.userData.splatter) return;
    const burst = owner.userData.burst || 0;
    const points = owner.userData.splatter;
    points.material.opacity = arch(burst) * 0.95;
    if (burst <= 0) return;
    const array = points.geometry.attributes.position.array;
    const dirs = owner.userData.splatterDirs;
    // Position is a pure function of burst progress, so scrubbing backwards
    // rewinds the burst instead of leaving particles stranded mid-flight.
    const spread = burst * 3.4;
    const fall = burst * burst * 2.6;
    for (let i = 0; i < dirs.length / 3; i += 1) {
      array[i * 3] = dirs[i * 3] * spread;
      array[i * 3 + 1] = dirs[i * 3 + 1] * spread - fall - 0.6;
      array[i * 3 + 2] = dirs[i * 3 + 2] * spread;
    }
    points.geometry.attributes.position.needsUpdate = true;
  }
}
