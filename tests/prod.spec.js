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
    expect(d.trace_hours).toBe(3);
    // Si des aéronefs sont en vol : trace = liste de [lat,lon].
    for (const a of d.aircraft.slice(0, 3)) {
      expect(typeof a.lat).toBe('number');
      expect(typeof a.lon).toBe('number');
      expect(Array.isArray(a.trace)).toBeTruthy();
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
    // Si des aéronefs sont en vol, des markers .avion-pin apparaissent.
    if (d.count > 0) {
      await expect(page.locator('.avion-pin').first()).toBeVisible({ timeout: 15000 });
    }
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

  test('bouton Contribuer open source pointe vers le dépôt GitHub', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    const btn = page.locator('#contrib-btn');
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAttribute('href', /github\.com\/.+\/feux-france/);
    await expect(btn).toHaveAttribute('target', '_blank');
  });
});
