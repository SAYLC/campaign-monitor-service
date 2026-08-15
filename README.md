# Bot d'alertes Whop vers Discord

Le bot vérifie toutes les campagnes Content Rewards au lancement, puis toutes
les 30 minutes. Il filtre les campagnes selon les critères définis et envoie
les résultats dans un salon Discord.

## Installation (une seule fois)

1. Double-clique sur `setup.bat`.
2. Attends la fin de l'installation.
3. Ouvre le fichier `.env` avec le Bloc-notes.
4. Remplace l'adresse d'exemple par ton webhook Discord.
5. Enregistre le fichier.
6. Double-clique sur `test_discord.bat` pour vérifier Discord.

## Lancement

Double-clique sur `start.bat`. Ne ferme pas la fenêtre noire : le bot effectue
une vérification immédiatement, puis attend 30 minutes.

Le bot ne démarre pas avec Windows. Si tu éteins le PC ou fermes la fenêtre,
il s'arrête. Au prochain lancement, il revérifie toutes les offres.

## Hébergement gratuit avec GitHub Actions

Le fichier `.github/workflows/whop-monitor.yml` lance automatiquement une
vérification toutes les 30 minutes. Le dépôt doit être public pour bénéficier
gratuitement des exécutions standard sans consommer le quota privé.

Dans GitHub, ouvre `Settings > Secrets and variables > Actions`, crée le secret
`DISCORD_WEBHOOK_URL` et colle le webhook comme valeur. Le webhook n'est jamais
écrit dans le dépôt.

L'historique est conservé dans le cache GitHub Actions. Il permet au bot de ne
pas renvoyer les mêmes campagnes et de modifier ou supprimer les anciens
messages Discord. Le code public ne contient ni le webhook, ni les informations
personnelles du compte GitHub.

## Critères configurés

- au moins 0,80 $ pour 1 000 vues ;
- budget total d'au moins 3 000 $ ;
- 30 % maximum de la cagnotte déjà utilisée (au moins 70 % restante) ;
- priorité aux cagnottes utilisées à 25 % ou moins ;
- à chaque passage, l'ancienne liste est remplacée par toutes les offres encore acceptables ;
- TikTok en priorité, YouTube Shorts accepté ;
- campagnes imposant moins de 3 heures signalées et écartées ;
- obligation de renommer le profil clairement signalée ;
- liens Whop et ressources Drive/Docs inclus lorsqu'ils sont visibles.

## Historique

L'historique se trouve dans `data/state.json`. Une campagne identique n'est pas
renvoyée à chaque lancement. Elle est renvoyée lorsque ses informations
importantes changent. Après chaque vérification réussie, les campagnes qui
n'existent plus ou qui ne respectent plus les critères sont supprimées de cet
historique et leur message Discord est également supprimé. Lorsqu'une campagne
change, son message existant est mis à jour au lieu d'en créer un deuxième.

## Confidentialité

Le webhook reste uniquement dans `.env`. Ne publie jamais ce fichier sur
GitHub et ne communique pas son contenu.
