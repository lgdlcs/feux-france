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

  test('bouton Contribuer open source pointe vers le dépôt GitHub', async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });
    const btn = page.locator('#contrib-btn');
    await expect(btn).toBeVisible();
    await expect(btn).toHaveAttribute('href', /github\.com\/.+\/feux-france/);
    await expect(btn).toHaveAttribute('target', '_blank');
  });
});
