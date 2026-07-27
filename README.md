# Feux France 🔥

Carte interactive de veille des incendies en France métropolitaine, en temps réel.

- **Détections satellites** : NASA FIRMS (VIIRS 375 m ×3 + MODIS 1 km), fenêtre stricte de 24 h, filtrage précis sur le territoire métropolitain (polygone, pas un rectangle), regroupement en foyers actifs avec emprise et surface estimées.
- **Vents animés** : particules façon Windfinder sur toute la France (grille Open-Meteo 0,5°, ~330 points, un seul appel batch), colorées par vitesse. En mode prévision, le champ affiche le **vent prévu** à l'instant sélectionné.
- **Prédictions** : panaches de propagation heure par heure par foyer (vent prévu intégré, plafonnés à 12 km), communes évacuées « sous le vent », indice de conditions de propagation — le tout étiqueté **estimation simplifiée, non officielle**.
- **Situation officielle & évacuations** : `situation.json` / `evacuations.json`, tenus à la main à partir des communiqués préfectoraux relayés par la presse ; chaque chiffre est sourcé et daté, l'inconnu s'affiche « n.c. ».
- **Zones brûlées** : délinéations officielles Copernicus EMS (CC-BY), chargées à la demande.
- **Frise chronologique** : −24 h → +12 h (zone prévision hachurée), lecture animée, scrub fluide (filtrage GPU, aucune donnée rechargée).
- Fonds Mapbox GL (Plan / Satellite / Relief), thèmes sombre et clair, mobile.

## Sources de données

| Donnée | Source | Rafraîchissement |
|---|---|---|
| Détections thermiques | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) (flux publics, sans clé) | 10 min (≈4 passages satellite/jour) |
| Météo & vents (live + prévisions 13 h) | [Open-Meteo](https://open-meteo.com/) | 10 min |
| Zones brûlées | [Copernicus EMS](https://emergency.copernicus.eu/) (CC-BY) | manuel (`public/burned.geojson`) |
| Communes | [geo.api.gouv.fr](https://geo.api.gouv.fr/) | à la demande (cache) |
| Situation / évacuations | communiqués préfectoraux via la presse | manuel, sourcé |

## Lancer en local

```bash
python3 server.py
# → http://localhost:8741
```

Aucune dépendance : Python stdlib côté serveur, Mapbox GL JS via CDN côté client. Actualisation automatique toutes les 10 min.

## Déployer

Le serveur écoute sur `0.0.0.0:$PORT` dès que la variable d'environnement `PORT` existe (fournie par l'hébergeur). Un `render.yaml` est inclus : sur [Render](https://render.com), « New → Blueprint » sur ce repo suffit (plan gratuit possible ; le service s'endort après 15 min d'inactivité — un ping type UptimeRobot le garde éveillé).

**Jeton Mapbox** : le jeton public (`pk.…`) inclus dans `public/index.html` doit être remplacé par le vôtre ([account.mapbox.com](https://account.mapbox.com), gratuit jusqu'à 50 000 chargements/mois) et **restreint à votre domaine** dans les réglages du jeton.

## Principe non négociable

**Aucune donnée inventée.** Les détections sont des anomalies thermiques satellites, pas des périmètres officiels ; les panaches et indices sont des estimations simplifiées clairement étiquetées ; seules les consignes préfectorales font foi. En cas de danger : **18 / 112**.

## Licence

[MIT](LICENSE). Données : NASA FIRMS (domaine public), Open-Meteo (CC-BY 4.0), Copernicus EMS (CC-BY), geo.api.gouv.fr (Licence Ouverte).
