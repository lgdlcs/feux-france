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

  test('activer le layer Points sensibles fait apparaître des pins', async ({ page }) => {
    // Ne teste le rendu que si le cache prod est peuplé.
    const d = await (await page.request.get(`${BASE}/api/sensibles`)).json();
    test.skip(!d.count, 'cache sensibles vide (warming) — rien à afficher');

    await page.goto(BASE, { waitUntil: 'networkidle' });
    await expect(page.locator('.mapboxgl-canvas')).toBeVisible({ timeout: 20000 });
    const toggle = page.locator('[data-l="sensibles"]');
    await expect(toggle).toBeVisible();
    await toggle.click();
    // Les pins sensibles portent la classe .sens-pin.
    await expect(page.locator('.sens-pin').first()).toBeVisible({ timeout: 15000 });
    const pins = await page.locator('.sens-pin').count();
    expect(pins).toBeGreaterThan(0);
  });
});
