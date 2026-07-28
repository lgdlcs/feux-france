// Tests de vérification post-déploiement contre la prod (carte-incendies.fr).
// Prérequis (package.json non versionné) : npm i -D @playwright/test && npx playwright install chromium
// Lancer : npx playwright test tests/prod.spec.js   (surcharger la cible : PROD_URL=http://localhost:8799)
// Vérifie : APIs (AROME/VPD/sensibles) + rendu carte (markers foyers + points sensibles).
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

  test('/api/sensibles répond (cache ou warming)', async ({ request }) => {
    const r = await request.get(`${BASE}/api/sensibles`);
    expect(r.ok()).toBeTruthy();
    const d = await r.json();
    // Structure toujours valide même quand le cache chauffe.
    expect(d).toHaveProperty('count');
    expect(d).toHaveProperty('items');
    expect(Array.isArray(d.items)).toBeTruthy();
    // Si peuplé : chaque item a lat/lon/cat.
    for (const it of d.items.slice(0, 5)) {
      expect(typeof it.lat).toBe('number');
      expect(typeof it.lon).toBe('number');
      expect(['sante', 'seveso']).toContain(it.cat);
    }
  });

  test('/api/sensibles?bbox renvoie les établissements de la zone', async ({ request }) => {
    // Zone PACA (Marseille–Toulon) : dense en santé + Seveso.
    // L'endpoint est en cache-puis-sert : la 1re requête peut répondre
    // building:true (count 0) le temps de construire en fond. On repolle.
    const BBOX = '5.2,43.2,6.2,43.9';
    let d = null;
    for (let i = 0; i < 30; i++) {
      const r = await request.get(`${BASE}/api/sensibles?bbox=${BBOX}`);
      expect(r.ok()).toBeTruthy();
      d = await r.json();
      expect(d).toHaveProperty('count');
      expect(Array.isArray(d.items)).toBeTruthy();
      if (!d.building && d.count > 0) break;
      if (d.count > 0) break;
      await new Promise((res) => setTimeout(res, 2000));
    }
    expect(d.count).toBeGreaterThan(0);
    for (const it of d.items.slice(0, 5)) {
      expect(it.lat).toBeGreaterThanOrEqual(43.2);
      expect(it.lat).toBeLessThanOrEqual(43.9);
    }
  });

  test('/api/sensibles?bbox refuse une emprise trop large', async ({ request }) => {
    const r = await request.get(`${BASE}/api/sensibles?bbox=-5,41,10,51`);
    expect(r.ok()).toBeTruthy();
    const d = await r.json();
    expect(d.too_wide).toBeTruthy();
    expect(d.items).toEqual([]);
  });

  test('/api/aircraft répond avec une structure valide', async ({ request }) => {
    const r = await request.get(`${BASE}/api/aircraft`);
    expect(r.ok()).toBeTruthy();
    const d = await r.json();
    expect(d).toHaveProperty('count');
    expect(Array.isArray(d.aircraft)).toBeTruthy();
    expect(d.trace_hours).toBe(3);
    // Si des aéronefs sont en vol : trace = liste de [lat,lon].
    for (const a of d.aircraft.slice(0, 3)) {
      expect(typeof a.lat).toBe('number');
      expect(typeof a.lon).toBe('number');
      expect(Array.isArray(a.trace)).toBeTruthy();
    }
  });
});

test.describe('Front prod', () => {
  test('la page se charge et embarque AROME + VPD', async ({ page }) => {
    const html = await (await page.request.get(BASE)).text();
    expect(html).toContain('meteofrance_arome');
    expect(html).toContain('vapour_pressure_deficit');
  });

  test('la carte affiche des markers de foyers', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    // Les foyers sont des markers Mapbox HTML ; on attend au moins un canvas Mapbox.
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    await page.waitForTimeout(3000); // laisse charger /api/fires + markers
    const markers = await page.locator('.mapboxgl-marker').count();
    expect(markers).toBeGreaterThan(0);
  });

  test('après zoom, le layer Points sensibles affiche des pins de la zone', async ({ page }) => {
    // Vérifie que la zone PACA est peuplée côté API (cache-puis-sert : on repolle
    // le temps que le build de fond aboutisse).
    let d = { count: 0 };
    for (let i = 0; i < 30; i++) {
      d = await (await page.request.get(`${BASE}/api/sensibles?bbox=5.2,43.2,6.2,43.9`)).json();
      if (d.count > 0) break;
      await new Promise((res) => setTimeout(res, 2000));
    }
    test.skip(!d.count, 'zone sans établissement collecté — rien à afficher');

    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    // La vue par défaut (France entière) est trop large : on zoome sur une zone dense.
    await page.evaluate(() => map.jumpTo({ center: [5.4, 43.3], zoom: 9 }));
    // Le fetch bbox (Overpass) peut prendre ~15 s.
    await expect(page.locator('.sens-pin').first()).toBeVisible({ timeout: 30000 });
    const pins = await page.locator('.sens-pin').count();
    expect(pins).toBeGreaterThan(0);
  });

  test('défauts : light mode, fond plan, toutes les couches actives', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    // Thème clair par défaut (aucun choix stocké, config système par défaut du navigateur de test).
    await expect(page.locator('body')).toHaveClass(/light/);
    // Fond « plan » actif.
    await expect(page.locator('.base-btn.on')).toHaveAttribute('data-b', 'plan');
    // Les 5 couches actives (vents, opérations, zones brûlées, points sensibles, moyens aériens).
    await expect(page.locator('.hud-layer.on')).toHaveCount(5);
  });

  test('moyens aériens : toggle présent, traces rendues si des aéronefs volent', async ({ page }) => {
    const d = await (await page.request.get(`${BASE}/api/aircraft`)).json();
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    // Le toggle « Moyens aériens » existe et est actif par défaut.
    await expect(page.locator('[data-l="avions"]')).toHaveClass(/on/);
    // Si des aéronefs sont en vol, des markers .avion-pin apparaissent.
    if (d.count > 0) {
      await expect(page.locator('.avion-pin').first()).toBeVisible({ timeout: 15000 });
    }
  });

  test('bouton Contribuer open source pointe vers le dépôt GitHub', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    const btn = page.locator('#contrib-btn');
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAttribute('href', /github\.com\/.+\/feux-france/);
    await expect(btn).toHaveAttribute('target', '_blank');
  });
});
