// Tests de vérification post-déploiement contre la prod (carte-incendies.fr).
// Prérequis (package.json non versionné) : npm i -D @playwright/test && npx playwright install chromium
// Lancer : npx playwright test tests/prod.spec.js   (surcharger la cible : PROD_URL=http://localhost:8799)
// Vérifie : APIs (AROME/VPD/aircraft) + rendu carte (markers foyers).
const { test, expect } = require('@playwright/test');

const BASE = process.env.PROD_URL || 'https://carte-incendies.fr';

test.describe('APIs prod', () => {
  test('/api/wind est peuplé (grille AROME)', async ({ request }) => {
    const r = await request.get(`${BASE}/api/wind`);
    expect(r.ok()).toBeTruthy();
    const d = await r.json();
    expect(d.count).toBeGreaterThan(0);
    expect(Array.isArray(d.points)).toBeTruthy();
  });

  test('/api/fires renvoie des foyers', async ({ request }) => {
    const r = await request.get(`${BASE}/api/fires`);
    expect(r.ok()).toBeTruthy();
    const d = await r.json();
    expect(Array.isArray(d.foyers)).toBeTruthy();
  });

  test('/api/aircraft répond avec une structure valide', async ({ request }) => {
    const r = await request.get(`${BASE}/api/aircraft`);
    expect(r.ok()).toBeTruthy();
    const d = await r.json();
    expect(d).toHaveProperty('count');
    expect(Array.isArray(d.aircraft)).toBeTruthy();
    expect(d.trace_hours).toBe(1);   // 3 h repliait la trace d'un Canadair sur elle-même
    // Si des aéronefs sont en vol : trace = liste de [lat,lon].
    for (const a of d.aircraft.slice(0, 3)) {
      expect(typeof a.lat).toBe('number');
      expect(typeof a.lon).toBe('number');
      expect(Array.isArray(a.trace)).toBeTruthy();
    }
  });

  test('/api/fires couvre bien 7 jours', async ({ request }) => {
    const r = await request.get(`${BASE}/api/fires`);
    const d = await r.json();
    expect(d.window_hours).toBe(168);
    // La plus ancienne détection doit dépasser les 24 h : sinon le flux servi
    // est resté en 24 h et la frise hebdomadaire n'a rien à rejouer.
    const oldest = Math.min(...d.points.map(p => Date.parse(p.acq_utc)));
    const ageH = (Date.now() - oldest) / 3.6e6;
    expect(ageH).toBeGreaterThan(24);
    expect(ageH).toBeLessThan(200);   // 168 h + la latence de publication FIRMS
    // Chaque foyer porte le cumul de la fenêtre ET le compte des 24 h, le
    // second ne pouvant pas dépasser le premier.
    for (const f of d.foyers.slice(0, 5)) {
      expect(typeof f.n_24h).toBe('number');
      expect(f.n_24h).toBeLessThanOrEqual(f.n);
    }
  });

  test('/api/fires répond vite, même à plusieurs en même temps', async ({ request }) => {
    // La panne du 30/07 : in_france() testait chaque point contre 31 292
    // sommets sans index, et get_data() laissait chaque visiteur lancer sa
    // propre collecte complète. Sur le petit CPU de l'hébergement, aucune ne
    // se terminait et /api/fires ne répondait plus jamais. Ce test vérifie le
    // contrat visible : la réponse arrive, et la concurrence ne l'écroule pas.
    const t0 = Date.now();
    const rs = await Promise.all([1, 2, 3, 4, 5].map(() => request.get(`${BASE}/api/fires`)));
    const dt = (Date.now() - t0) / 1000;
    for (const r of rs) expect(r.ok()).toBeTruthy();
    expect(dt).toBeLessThan(25);   // large : c'est l'écroulement qu'on traque, pas la milliseconde
    const d = await rs[0].json();
    expect(d.count).toBeGreaterThan(0);
  });

  test('les périmètres EFFIS sont régénérés, pas figés', async ({ request }) => {
    const r = await request.get(`${BASE}/burned.geojson`);
    expect(r.ok()).toBeTruthy();
    const d = await r.json();
    // Marqueur de notre propre générateur : un fichier importé à la main ne
    // l'a pas, ce qui signalerait que la régénération ne tourne plus.
    expect(d.generator).toBe('effis-wfs');
    expect(d.features.length).toBeGreaterThan(200);
    // Moins de 7 jours : EFFIS republie 1 à 2 fois par jour, le serveur toutes
    // les 6 h. Au-delà, la tâche de fond est tombée.
    const ageDays = (Date.now() - Date.parse(d.generated_at_utc)) / 864e5;
    expect(ageDays).toBeLessThan(7);
    // Les deux couches sont présentes et les polygones datés sont renseignés.
    const kinds = new Set(d.features.map(f => f.properties.kind));
    expect(kinds.has('dated')).toBeTruthy();
    const dated = d.features.filter(f => f.properties.kind === 'dated');
    expect(dated.some(f => f.properties.area_ha > 0 && f.properties.date)).toBeTruthy();
    // Axes non inversés : tout doit tomber dans l'emprise métropole. Le piège
    // du WFS EFFIS ([lat, lon] au lieu de [lon, lat]) enverrait ça en Somalie.
    const lons = [], lats = [];
    const walk = x => {
      if (!Array.isArray(x) || !x.length) return;
      if (typeof x[0] === 'number') { lons.push(x[0]); lats.push(x[1]); }
      else x.forEach(walk);
    };
    d.features.forEach(f => walk(f.geometry.coordinates));
    expect(Math.min(...lons)).toBeGreaterThan(-6);
    expect(Math.max(...lons)).toBeLessThan(10);
    expect(Math.min(...lats)).toBeGreaterThan(41);
    expect(Math.max(...lats)).toBeLessThan(52);
  });
});

test.describe('Front prod', () => {
  test('la page se charge et embarque AROME + VPD', async ({ page }) => {
    const html = await (await page.request.get(BASE)).text();
    expect(html).toContain('meteofrance_arome');
    expect(html).toContain('vapour_pressure_deficit');
  });

  test('la carte rend les détections en WebGL', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    await page.waitForTimeout(6000); // laisse charger /api/fires
    // Depuis la migration WebGL il n'y a plus aucun marker HTML : détections,
    // foyers et aéronefs sont des couches Mapbox. On interroge donc le rendu.
    const rendu = await page.evaluate(() => ({
      couches: map.getStyle().layers.map(l => l.id).filter(id => /^(fires|foyers|avions)/.test(id)),
      detections: map.queryRenderedFeatures({ layers: ['fires'] }).length,
    }));
    expect(rendu.couches).toContain('fires');
    expect(rendu.couches).toContain('foyers-icon');
    expect(rendu.detections).toBeGreaterThan(0);
  });

  test('défauts : light mode, fond plan, toutes les couches actives', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    // Thème clair par défaut (aucun choix stocké, config système par défaut du navigateur de test).
    await expect(page.locator('body')).toHaveClass(/light/);
    // Fond « plan » actif.
    await expect(page.locator('.base-btn.on')).toHaveAttribute('data-b', 'plan');
    // Les 4 couches actives (vents, opérations, zones brûlées, moyens aériens).
    await expect(page.locator('.hud-layer.on')).toHaveCount(4);
  });

  test('moyens aériens : toggle présent, traces rendues si des aéronefs volent', async ({ page }) => {
    const d = await (await page.request.get(`${BASE}/api/aircraft`)).json();
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    // Le toggle « Moyens aériens » existe et est actif par défaut.
    await expect(page.locator('[data-l="avions"]')).toHaveClass(/on/);
    // Les aéronefs sont eux aussi rendus en WebGL (plus de markers HTML) : on
    // vérifie la couche, et les positions seulement si des appareils volent.
    await page.waitForTimeout(6000);
    const av = await page.evaluate(() => ({
      couche: !!map.getLayer('avions-icons'),
      positions: map.getSource('avions-pos')?._data?.features?.length ?? 0,
    }));
    expect(av.couche).toBeTruthy();
    if (d.count > 0) expect(av.positions).toBeGreaterThan(0);
  });

  test('vignette de partage : balises Open Graph + Twitter Card servies', async ({ page }) => {
    const html = await (await page.request.get(BASE)).text();
    // Balises meta présentes dans le <head>.
    expect(html).toContain('property="og:title"');
    expect(html).toContain('property="og:image"');
    expect(html).toContain('https://carte-incendies.fr/og-image.jpg');
    expect(html).toContain('name="twitter:card"');
    expect(html).toContain('summary_large_image');
    // L'image OG existe réellement, est un JPEG et fait ~1200×630.
    const img = await page.request.get(`${BASE}/og-image.jpg`);
    expect(img.ok()).toBeTruthy();
    expect(img.headers()['content-type']).toContain('image/jpeg');
    const buf = await img.body();
    expect(buf.length).toBeGreaterThan(10000); // pas un fichier vide/placeholder
  });

  test('la frise couvre 7 jours et rejoue la semaine', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    await page.waitForTimeout(6000);   // laisse arriver /api/fires
    const st = await page.evaluate(() => ({
      PAST_H, VIEW_H, SLIDER_NOW, SLIDER_MAX,
      days: document.querySelectorAll('#tl-track .tl-day').length,
      bars: document.querySelectorAll('#tl-track .tl-mbar').length,
      points: allPoints.length,
    }));
    expect(st.PAST_H).toBe(168);
    expect(st.VIEW_H).toBe(24);
    expect(st.SLIDER_MAX).toBe(st.SLIDER_NOW + 24);   // +12 h au pas de 30 min
    expect(st.days).toBeGreaterThanOrEqual(6);        // un séparateur par jour
    expect(st.bars).toBeGreaterThan(10);
    expect(st.points).toBeGreaterThan(1000);
    // Scrub 5 jours en arrière : la semaine est réellement rejouable.
    const back = await page.evaluate(() => {
      setSlider(96);
      const T = currentT(), ws = T - VIEW_H * 3.6e6;
      return { visibles: allPoints.filter(p => p._ms <= T && p._ms > ws).length,
               bulle: document.querySelector('.tl-bubble').textContent };
    });
    expect(back.visibles).toBeGreaterThan(0);
    expect(back.bulle).not.toBe('MAINTENANT');
  });

  test('la vue de l\'utilisateur est mémorisée d\'une visite à l\'autre', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    // Vue par défaut : la métropole entière.
    expect(await page.evaluate(() => +map.getZoom().toFixed(1))).toBeLessThan(6);
    // L'utilisateur se déplace sur un feu.
    await page.evaluate(async () => {
      map.jumpTo({ center: [-1.15, 44.85], zoom: 10 });
      await new Promise(r => setTimeout(r, 1200));   // laisse passer moveend
    });
    // Il revient : on le remet où il était, pas sur la France entière.
    await page.reload({ waitUntil: 'networkidle' });
    const v = await page.evaluate(() => ({
      zoom: +map.getZoom().toFixed(2),
      lon: +map.getCenter().lng.toFixed(2),
      lat: +map.getCenter().lat.toFixed(2),
    }));
    expect(v.zoom).toBeCloseTo(10, 1);
    expect(v.lon).toBeCloseTo(-1.15, 1);
    expect(v.lat).toBeCloseTo(44.85, 1);
    // Un stockage corrompu ou aberrant ne doit pas expédier la carte ailleurs.
    const rejets = await page.evaluate(() => {
      const cas = ['pas du json', JSON.stringify({ center: [-140, 12], zoom: 9 }),
                   JSON.stringify({ center: [999, 'x'], zoom: null })];
      return cas.map(c => { localStorage.setItem('ff-vue', c); return vueMemorisee(); });
    });
    expect(rejets).toEqual([null, null, null]);
    await page.reload({ waitUntil: 'networkidle' });
    expect(await page.evaluate(() => +map.getZoom().toFixed(1))).toBeLessThan(6);
  });

  test('les détections rapetissent en vue nationale', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    // Le rayon doit dépendre du zoom : sinon une détection couvre ~50 km en vue
    // France et les points se fondent en pâtés qui masquent la carte.
    const r = await page.evaluate(() => {
      const expr = map.getPaintProperty('fires', 'circle-radius');
      return { expr: JSON.stringify(expr), zoomDependant: JSON.stringify(expr).includes('"zoom"') };
    });
    expect(r.zoomDependant).toBeTruthy();
  });

  test('bouton Contribuer open source pointe vers le dépôt GitHub', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    const btn = page.locator('#contrib-btn');
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAttribute('href', /github\.com\/.+\/carte-incendies/);
    await expect(btn).toHaveAttribute('target', '_blank');
  });

  // Régression : sous Windows, l'économiseur de batterie coupe les animations système,
  // donc prefers-reduced-motion passe à `reduce` sans que le visiteur l'ait demandé.
  // La carte tombait alors sur la grille de points statiques sans aucun recours.
  test.describe('vent en prefers-reduced-motion', () => {
    test.use({ contextOptions: { reducedMotion: 'reduce' } });

    test('points statiques par défaut, mais animation réactivable et mémorisée', async ({ page }) => {
      await page.goto(BASE, { waitUntil: 'networkidle' });
      await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
      await page.waitForTimeout(8000); // laisse charger /api/wind

      const etat = () => page.evaluate(() => ({
        canvas: getComputedStyle(document.getElementById('wind-canvas')).display,
        anime: typeof windRaf !== 'undefined' && windRaf !== null,
        opacite: map.getPaintProperty('wind-dots', 'circle-opacity'),
      }));

      // par défaut on respecte la préférence système
      expect(await etat()).toMatchObject({ canvas: 'none', anime: false, opacite: 0.85 });

      // ...et la légende propose explicitement de réactiver
      const btn = page.locator('#wl-motion-btn');
      await expect(page.locator('#wl-motion')).toHaveClass(/\bon\b/);
      await expect(btn).toBeVisible();
      await btn.click();
      await page.waitForTimeout(2000);
      expect(await etat()).toMatchObject({ canvas: 'block', anime: true, opacite: 0.01 });

      // le choix survit au rechargement
      await page.reload({ waitUntil: 'networkidle' });
      await page.waitForTimeout(8000);
      expect(await etat()).toMatchObject({ canvas: 'block', anime: true });
    });
  });
});
