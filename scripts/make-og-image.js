// Génère public/og-image.png (1200×630) : vignette de partage réseaux sociaux.
// Vue satellite zoomée sur la Gironde + titre « Carte Incendies ».
//
// Le token Mapbox est restreint au domaine carte-incendies.fr : on charge donc
// une page HTML minimale SERVIE PAR LA PROD (origine autorisée) qui rend la carte,
// puis on capture un screenshot. Lancer : node scripts/make-og-image.js
const { chromium } = require('@playwright/test');
const path = require('path');

const TOKEN = 'pk.eyJ1IjoibHVjYXN1cGNhc2UiLCJhIjoiY21zM2hyamQ2MDN2NTMwcXpweHpvcW5kbyJ9.3zYG1qNlW4rBXoTZ7H1eHg';
const OUT = path.join(__dirname, '..', 'public', 'og-image.jpg');
const W = 1200, H = 630;

// Gironde : Bordeaux ≈ -0.58/44.84, bassin d'Arcachon ≈ -1.16/44.66.
// Centre un peu à l'ouest pour cadrer la forêt des Landes/la côte (zones d'incendies 2022).
const CENTER = [-0.75, 44.72];
const ZOOM = 7.6;

const PAGE_HTML = `<!doctype html><html><head><meta charset="utf-8">
<link href="https://api.mapbox.com/mapbox-gl-js/v3.4.0/mapbox-gl.css" rel="stylesheet">
<script src="https://api.mapbox.com/mapbox-gl-js/v3.4.0/mapbox-gl.js"></script>
<style>
  html,body{margin:0;padding:0;width:${W}px;height:${H}px;overflow:hidden;background:#0a0a0a;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
  #map{position:absolute;inset:0}
  /* Dégradé sombre en bas pour la lisibilité du texte */
  #veil{position:absolute;inset:0;pointer-events:none;
    background:linear-gradient(180deg,rgba(0,0,0,.15) 0%,rgba(0,0,0,0) 35%,rgba(0,0,0,.25) 62%,rgba(0,0,0,.82) 100%)}
  #brand{position:absolute;left:56px;bottom:52px;right:56px;pointer-events:none}
  #title{font-size:82px;font-weight:800;color:#fff;letter-spacing:-1.5px;line-height:1;
    text-shadow:0 2px 24px rgba(0,0,0,.6)}
  #title .fire{color:#ff5722}
  #subtitle{margin-top:16px;font-size:30px;font-weight:500;color:#f0f0f0;
    text-shadow:0 1px 12px rgba(0,0,0,.7)}
  #domain{position:absolute;right:56px;top:44px;font-size:26px;font-weight:600;color:#fff;
    background:rgba(0,0,0,.45);padding:8px 18px;border-radius:999px;backdrop-filter:blur(4px);
    text-shadow:0 1px 8px rgba(0,0,0,.5)}
  #flame{font-size:78px;line-height:1;text-shadow:0 2px 24px rgba(0,0,0,.6)}
  /* Masque le logo Mapbox (l'attribution reste requise ailleurs sur le site) */
  .mapboxgl-ctrl-logo,.mapboxgl-ctrl-attrib,.mapboxgl-ctrl-bottom-left,.mapboxgl-ctrl-bottom-right{display:none!important}
</style></head><body>
<div id="map"></div>
<div id="veil"></div>
<div id="domain">🔥 carte-incendies.fr</div>
<div id="brand">
  <div id="title">Carte <span class="fire">Incendies</span></div>
  <div id="subtitle">Les feux en France en temps réel — satellites, vents & périmètres officiels</div>
</div>
<script>
  mapboxgl.accessToken = '${TOKEN}';
  window.__ready = false;
  const map = new mapboxgl.Map({
    container:'map',
    style:'mapbox://styles/mapbox/satellite-streets-v12',
    center:[${CENTER[0]}, ${CENTER[1]}],
    zoom:${ZOOM},
    interactive:false,
    attributionControl:false,
    fadeDuration:0,
  });
  map.on('idle', () => { window.__ready = true; });
</script></body></html>`;

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: W, height: H },
    deviceScaleFactor: 1, // capture exactement 1200×630 (taille OG recommandée)
  });
  // On sert la page depuis l'origine de prod pour que le token Mapbox soit autorisé.
  await page.route('https://carte-incendies.fr/__og__', route =>
    route.fulfill({ contentType: 'text/html', body: PAGE_HTML }));
  await page.goto('https://carte-incendies.fr/__og__', { waitUntil: 'load' });

  // Attend que les tuiles satellite soient chargées (event 'idle').
  await page.waitForFunction(() => window.__ready === true, { timeout: 60000 });
  await page.waitForTimeout(1500); // marge pour le rendu final des labels

  await page.screenshot({ path: OUT, type: 'jpeg', quality: 82, clip: { x: 0, y: 0, width: W, height: H } });
  await browser.close();
  console.log('OK →', OUT);
})().catch(e => { console.error('ÉCHEC', e); process.exit(1); });
