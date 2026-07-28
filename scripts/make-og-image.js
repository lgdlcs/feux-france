// Génère public/og-image.jpg (1200×630) : vignette de partage réseaux sociaux.
// Fond : vraie capture du site (scripts/og-bg.jpg — feux de Gironde, vents animés,
// zones brûlées, HUD) + nom du site en très gros. Lancer : node scripts/make-og-image.js
const { chromium } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const BG = path.join(__dirname, 'og-bg.jpg');
const OUT = path.join(__dirname, '..', 'public', 'og-image.jpg');
const W = 1200, H = 630;

const PAGE_HTML = `<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@500;600&display=swap" rel="stylesheet">
<style>
  html,body{margin:0;padding:0;width:${W}px;height:${H}px;overflow:hidden;background:#0a0a0a}
  /* Capture réelle du site, cadrée sur le foyer (gauche) en gardant le HUD (droite) */
  #bg{position:absolute;inset:0;background:url('file://${BG}') center 42%/cover no-repeat}
  /* Voile : lisibilité du titre en bas, image claire au centre */
  #veil{position:absolute;inset:0;pointer-events:none;
    background:linear-gradient(180deg,rgba(10,10,10,.28) 0%,rgba(10,10,10,0) 30%,
      rgba(10,10,10,0) 48%,rgba(10,10,10,.55) 78%,rgba(10,10,10,.92) 100%)}
  #brand{position:absolute;left:56px;bottom:40px;right:56px;pointer-events:none}
  #title{font-family:'Barlow Condensed',sans-serif;font-size:132px;font-weight:700;
    color:#fff;letter-spacing:.5px;line-height:.95;text-transform:uppercase;
    text-shadow:0 3px 30px rgba(0,0,0,.75)}
  #title .fire{color:#ff5a2b}
  #subtitle{margin-top:12px;font-family:'Barlow',sans-serif;font-size:31px;font-weight:500;
    color:#f2f2f2;text-shadow:0 1px 14px rgba(0,0,0,.8)}
  #domain{position:absolute;left:56px;top:40px;font-family:'Barlow',sans-serif;
    font-size:27px;font-weight:600;color:#fff;background:rgba(10,10,10,.55);
    padding:9px 20px;border-radius:999px;text-shadow:0 1px 8px rgba(0,0,0,.5)}
  #live{position:absolute;right:56px;top:44px;font-family:'Barlow Condensed',sans-serif;
    font-size:26px;font-weight:700;letter-spacing:.14em;color:#fff;background:#d92c0f;
    padding:8px 18px;border-radius:8px;box-shadow:0 2px 18px rgba(0,0,0,.45)}
</style></head><body>
<div id="bg"></div>
<div id="veil"></div>
<div id="domain">🔥 carte-incendies.fr</div>
<div id="live">● DIRECT</div>
<div id="brand">
  <div id="title">Carte <span class="fire">Incendies</span></div>
  <div id="subtitle">Les feux en France en temps réel — satellites NASA, vents, moyens aériens & périmètres officiels</div>
</div>
<script>
  window.__ready = false;
  Promise.all([
    document.fonts.load("700 132px 'Barlow Condensed'"),
    document.fonts.load("500 31px 'Barlow'"),
    new Promise(res => { const i = new Image(); i.onload = res; i.onerror = res; i.src = 'file://${BG}'; }),
  ]).then(() => setTimeout(() => { window.__ready = true; }, 300));
</script></body></html>`;

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: W, height: H },
    deviceScaleFactor: 1, // capture exactement 1200×630 (taille OG recommandée)
  });
  // Page servie en file:// (une page data: n'a pas le droit de charger l'image locale).
  const tmp = path.join(__dirname, '.og-tmp.html');
  fs.writeFileSync(tmp, PAGE_HTML);
  await page.goto('file://' + tmp, { waitUntil: 'load' });
  await page.waitForFunction(() => window.__ready === true, { timeout: 30000 });
  await page.screenshot({ path: OUT, type: 'jpeg', quality: 85, clip: { x: 0, y: 0, width: W, height: H } });
  await browser.close();
  fs.unlinkSync(tmp);
  console.log('OK →', OUT);
})().catch(e => { console.error('ÉCHEC', e); process.exit(1); });
