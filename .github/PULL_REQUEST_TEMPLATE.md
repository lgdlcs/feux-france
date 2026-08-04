<!--
Merci pour la contribution. Le gabarit ci-dessous sert surtout aux mises à jour de
données (situation.json / evacuations.json), qui sont l'essentiel des PR reçues.
Pour un changement de code, remplacez-le librement par une description.
-->

## Ce que change cette PR

<!-- Une ou deux phrases. Ex. : « ajoute les retours d'habitants en Gironde, du 28 juillet au 2 août ». -->

## Sources

<!--
Une ligne par source, avec le lien. Règle non négociable du projet :
pas un chiffre sans source, pas une estimation sans étiquette.
-->

- 

## Vérifications

- [ ] `python3 scripts/check_data.py` passe (valide le JSON et le format des entrées)
- [ ] chaque entrée porte `source` **et** `source_url`
- [ ] `dept` est cohérent avec la commune (`"Landes (40)"`, pas le département voisin)
- [ ] `annonce` est la date de l'annonce officielle, pas celle de la saisie
- [ ] `updated` en tête de fichier est remonté à la date de la dernière entrée ajoutée
- [ ] aucun commentaire dans le JSON — voir le format ci-dessous

## Format attendu des données

Le JSON n'accepte **ni commentaire ni virgule traînante**. Un fichier invalide n'est
pas rejeté bruyamment : le serveur avale l'exception et sert `evacuations: null`,
donc le panneau part vide en production sans erreur nulle part. D'où le script de
vérification.

Pour regrouper des entrées, ne pas insérer d'objets séparateurs dans les tableaux
(ils seraient rendus comme des lignes vides) : le champ `dept` porte déjà le
regroupement.

```json
{
  "commune": "Biscarrosse", "dept": "Landes (40)", "lat": 44.4077, "lon": -1.1525,
  "population": null, "statut": "évacuation totale",
  "annonce": "2026-07-24",
  "source": "Préfecture des Landes",
  "source_url": "https://www.landes.gouv.fr/…"
}
```

| Champ | Attendu |
|---|---|
| `commune` | nom officiel, ou libellé explicite pour un lieu (`"autoroute A63"`, `"EHPAD Les Tchanqués (Lège)"`) |
| `dept` | `"Nom (NN)"` — celui de la commune concernée |
| `lat` / `lon` | centre officiel [geo.api.gouv.fr](https://geo.api.gouv.fr/communes), 4 décimales |
| `population` | entier, ou `null` si non communiquée — jamais une estimation |
| `statut` | ce qu'annonce la préfecture, repris au plus près |
| `annonce` | `"AAAA-MM-JJ"`, précision libre entre parenthèses (`"2026-07-25 (soir)"`) |
| `source` | l'autorité, et le relais s'il y en a un (`"Préfecture de la Gironde via franceinfo"`) |
| `source_url` | lien direct vers le communiqué ou l'article |
