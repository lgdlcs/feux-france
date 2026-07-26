# Feux France 🔥

Carte interactive de veille des incendies en France métropolitaine, en temps réel.

- **Détections satellites** : NASA FIRMS (VIIRS 375 m ×3 + MODIS 1 km), fenêtre stricte de 24 h, filtrage précis sur le territoire métropolitain, regroupement en foyers actifs avec emprise réelle.
- **Météo & prévisions** : Open-Meteo (conditions live + prévisions 12 h par foyer, cônes de propagation probable basés sur le vent — estimation simplifiée, non officielle).
- **Situation officielle & évacuations** : `situation.json` / `evacuations.json`, tenus à la main à partir des communiqués préfectoraux relayés par la presse ; chaque chiffre est sourcé et daté, l'inconnu s'affiche « n.c. ».
- **Frise chronologique** : −24 h → +12 h (zone prévision), lecture animée.

## Lancer

```bash
python3 server.py
# → http://localhost:8741
```

Aucune dépendance (Python stdlib + Leaflet via CDN). Actualisation automatique toutes les 10 min.

**Principe non négociable : aucune donnée inventée.** Les détections sont des anomalies thermiques satellites, pas des périmètres officiels. En cas de danger : 18 / 112.
