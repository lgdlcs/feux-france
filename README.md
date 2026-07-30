# Carte Incendies 🔥

Carte interactive de veille des incendies en France métropolitaine, en temps réel.

**🌍 Site officiel (temps réel)** : **https://carte-incendies.fr**

- **Détections satellites** : NASA FIRMS (VIIRS 375 m ×3 + MODIS 1 km), fenêtre glissante de **7 jours** (le maximum des flux publics, sans clé API), filtrage précis sur le territoire métropolitain, regroupement en foyers actifs avec emprise estimée. Chaque foyer porte son cumul sur 7 j et son compte des dernières 24 h.
- **Vents animés** : particules façon Windfinder, colorées par vitesse, pilotées par la frise (vent actuel ou vent **prévu** à l'instant sélectionné). Données Open-Meteo, grille 0,5° sur la métropole, un seul appel batch, cache 10 min.
- **Prédictions** : panaches de propagation heure par heure par foyer (vent prévu intégré), communes évacuées « sous le vent », indice de conditions de propagation — **estimations simplifiées, non officielles**, toujours étiquetées comme telles.
- **Zones brûlées** : périmètres officiels **EFFIS / Copernicus**, régénérés en tâche de fond toutes les 6 h — polygones **datés** de la saison (avec surface et commune) et contours **NRT** plus frais mais sans attribut, tracés plus discrètement. Régénération manuelle : `python3 scripts/fetch_burned.py`.
- **Situation officielle & évacuations** : `situation.json` / `evacuations.json`, tenus à la main à partir des communiqués préfectoraux relayés par la presse ; chaque chiffre est sourcé et daté, l'inconnu s'affiche « n.c. ».
- **Frise chronologique** : **−7 j → +12 h** (zone prévision), lecture animée de la semaine, scrub fluide (filtrage GPU). À un instant donné on affiche 24 h de détections, pour qu'un grand feu ne devienne pas une tache uniforme.

**Principe non négociable : aucune donnée inventée.** Les détections sont des anomalies thermiques satellites, pas des périmètres officiels. Seules les consignes préfectorales font foi. En cas de danger : **18 / 112**.

## Lancer en local

```bash
python3 server.py
# → http://localhost:8741
```

Aucune dépendance (Python stdlib ; Mapbox GL JS via CDN). Actualisation automatique toutes les 10 min.

## Déployer

Le serveur lit `PORT` et `HOST` dans l'environnement (défauts : 8741 / 127.0.0.1 en local, 0.0.0.0 dès que `PORT` est défini).

- **Render** : `render.yaml` inclus — « New + » → Blueprint → ce repo (plan gratuit possible, avec mise en veille après inactivité).
- **Docker** : `docker build -t feux-france . && docker run -p 8741:8741 feux-france` (fonctionne sur Fly.io, Railway, un VPS…).

### Clé Mapbox

Le frontend utilise un jeton public Mapbox (`pk.…` dans `public/index.html`). Si vous déployez votre propre instance, créez votre jeton sur [account.mapbox.com](https://account.mapbox.com) et **restreignez-le à votre domaine** (URL restrictions). Le plan gratuit Mapbox (50 000 chargements de carte/mois) suffit largement.

## Sources de données

| Donnée | Source | Licence / accès |
|---|---|---|
| Détections thermiques | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) (flux publics 7 j, sans clé) | domaine public |
| Météo & vents | [Open-Meteo](https://open-meteo.com/) | CC-BY 4.0, sans clé |
| Zones brûlées | [EFFIS / Copernicus](https://forest-fire.emergency.copernicus.eu/) (WFS, sans clé) | CC-BY |
| Situation / évacuations | communiqués préfectoraux via presse, saisis à la main | sourcé entrée par entrée |
| Fonds de carte | Mapbox / OpenStreetMap | jeton public Mapbox |

## Contribuer

Les PR sont bienvenues — en particulier la mise à jour de `situation.json` / `evacuations.json` (toujours avec source et date) pendant les épisodes de feux. Toute contribution doit respecter la règle : pas de chiffre sans source, pas d'estimation sans étiquette.

## Licence

[MIT](LICENSE) — © 2026 Lucas Legrand.
