# StrategyProdV8 Nautilus

Prototype de production Python pour porter `StrategyProdV8` vers:

- Databento pour les trades/quotes;
- NautilusTrader pour le moteur live;
- IBKR pour l'execution;
- Docker Desktop pour logs, exec et stats.

Le dossier est volontairement minimal: pas de cockpit web, pas de SQLite obligatoire.

## Commandes Docker Exec

Quand le container tourne, utiliser:

```sh
algoctl status
algoctl pause
algoctl resume
algoctl stopflat
algoctl flatten
```

Ces commandes modifient `/app/control/v8_es.json`. La strategie observe ce fichier et applique les changements.

### Detail des commandes `algoctl`

`algoctl status`

Affiche tout le fichier de controle actuel. C'est la commande a utiliser pour voir l'etat complet: autorisation des entrees, sizing, demandes de flatten, etc.

`algoctl risk`

Affiche seulement les parametres de sizing:

```json
{
  "contracts": 3,
  "enable_auto_sizing": false,
  "sizing_r_multiple": 1.25,
  "max_contracts": 3
}
```

`algoctl pause`

Met `allow_new_entries=false`.

Effet: l'algo ne prend plus de nouvelles entrees, mais il ne coupe pas une position deja ouverte. Si un trade est deja en cours, le TP/SL continue de gerer la sortie.

`algoctl resume`

Remet:

```json
{
  "allow_new_entries": true,
  "flatten_now": false,
  "stop_after_flat": false
}
```

Effet: l'algo peut reprendre les nouveaux trades.

`algoctl stopflat`

Met:

```json
{
  "allow_new_entries": false,
  "stop_after_flat": true
}
```

Effet: l'algo ne prend plus de nouvelles entrees et, si une position est ouverte, il attend qu'elle se ferme normalement. Une fois flat, il reste en pause.

`algoctl flatten`

Met:

```json
{
  "allow_new_entries": false,
  "flatten_now": true
}
```

Effet: l'algo demande l'annulation des ordres actifs et tente de fermer la position au marche. A utiliser si tu veux sortir tout de suite.

`algoctl contracts N`

Exemple:

```sh
algoctl contracts 2
```

Met `contracts=2`.

Effet: change le nombre de contrats en mode sizing manuel. Cette valeur est bornee par `max_contracts`.

`algoctl max N`

Exemple:

```sh
algoctl max 3
```

Met `max_contracts=3`.

Effet: fixe le plafond absolu de contrats autorises par le controle live.

`algoctl autosizing on`

Met `enable_auto_sizing=true`.

Effet: active le mode auto sizing cote controle. Pour l'instant, le code reste conservateur et ne devine pas l'equity IBKR: il retombe sur `contracts` tant que le vrai calcul d'equity n'est pas cable.

`algoctl autosizing off`

Met `enable_auto_sizing=false`.

Effet: revient au sizing manuel via `contracts`.

`algoctl rmultiple N`

Exemple:

```sh
algoctl rmultiple 1.25
```

Met `sizing_r_multiple=1.25`.

Effet: prepare le multiplicateur de risque pour l'auto sizing. Il est stocke et logge, mais il ne pilote pas encore le nombre de contrats tant que l'equity IBKR n'est pas cablee.

## Bonnes pratiques d'arret live

Le container ne doit pas etre coupe brutalement pendant qu'un trade est en cours. Le risque principal est de couper entre le fill d'entree et la pose du bracket TP/SL, ou pendant une modification du stop.

### Pause simple

Pour empecher les nouveaux trades sans toucher a une position deja ouverte:

```sh
docker exec -it strategy-prod-v8-es algoctl pause
```

Effet attendu:

- l'algo ne prend plus de nouvelles entrees;
- une position deja ouverte reste geree par ses ordres TP/SL;
- le container continue de tourner.

### Arret normal recommande

Pour arreter proprement sans forcer la sortie au marche:

```sh
docker exec -it strategy-prod-v8-es algoctl stopflat
docker logs -f strategy-prod-v8-es
```

Attendre ensuite dans les logs:

```text
POSITION_FLAT
```

Puis verifier dans IBKR:

- position ES = 0;
- aucun ordre ouvert restant pour l'algo.

Seulement apres:

```sh
docker stop strategy-prod-v8-es
```

### Sortie urgente

Pour demander une sortie immediate:

```sh
docker exec -it strategy-prod-v8-es algoctl flatten
docker logs -f strategy-prod-v8-es
```

L'algo tente d'abord d'annuler les ordres actifs, puis ferme la position au marche. Apres `POSITION_FLAT`, verifier aussi dans IBKR que position = 0 et ordres ouverts = 0.

### Ce qu'il faut eviter

Eviter d'arreter le container si les logs indiquent:

```text
ENTRY_PENDING
ENTRY_PARTIAL
IN_POSITION
FLATTEN_PENDING
BRACKET_SUBMIT_REQUEST
PROTECTED_STOP_REQUEST
```

Eviter aussi:

```sh
docker kill strategy-prod-v8-es
```

`docker kill` coupe brutalement le process. Preferer toujours `docker stop`, et seulement quand l'algo est flat.

### Apres crash ou coupure

Si le container, Docker, IB Gateway ou le VPS s'arrete de maniere non prevue:

1. ouvrir IBKR Desktop, mobile ou TWS;
2. verifier la position ES;
3. verifier les ordres ouverts;
4. si une position existe, la gerer manuellement ou verifier que TP/SL sont bien actifs;
5. relancer l'algo seulement une fois position = 0 et ordres ouverts = 0.

L'algo ne fait pas encore de reconciliation complete au demarrage. Il ne faut donc pas le relancer aveuglement s'il reste une position ou des ordres orphelins chez IBKR.

## Fichiers importants

- `src/config.py`: configuration statique strategie/data/execution en Python oriente objet.
- `src/instruments.py`: resolution du front-month concret pour Databento et IBKR.
- `control/v8_es.json`: etat demande par l'utilisateur.
- `scripts/algoctl`: commandes courtes pour Docker Exec.
- `src/v8_engine.py`: moteur V8 pur, independant de Nautilus.
- `src/strategy.py`: strategie Nautilus.
- `src/run_live.py`: point d'entree live.
- `algoctl-vps.cmd`: commande hote Windows pour choisir le mode paper/live et lancer le container.
- `scripts/algoctl-vps.ps1`: implementation PowerShell appelee par `algoctl-vps.cmd`.

## Choisir Paper ou Live

Par defaut, l'algo demarre en `paper`.

Le mode est lu au demarrage du container avec:

```env
ALGO_TRADING_MODE=paper
```

ou:

```env
ALGO_TRADING_MODE=live
```

Les comptes peuvent etre separes:

```env
TWS_ACCOUNT_PAPER=DU123456
TWS_ACCOUNT_LIVE=U1234567
```

Le mode recommande est de definir `TWS_ACCOUNT_PAPER` et `TWS_ACCOUNT_LIVE` en variables utilisateur Windows. `TWS_ACCOUNT` est seulement l'etat actif ecrit par `algoctl-vps` dans `.env` au moment du lancement.

Pour changer de mode depuis le VPS avec une commande courte et lancer automatiquement le container:

PowerShell:

```powershell
.\scripts\algoctl-vps.ps1 PROD-ES PAPER
```

ou:

```powershell
.\scripts\algoctl-vps.ps1 PROD-ES LIVE
```

Version encore plus simple depuis la racine du dossier V8:

```powershell
.\algoctl-vps.cmd PROD-ES PAPER
```

ou:

```powershell
.\algoctl-vps.cmd PROD-ES LIVE
```

Si tu veux taper exactement `algoctl-vps PROD-ES LIVE` sans `.\`, ajoute le dossier V8 a ton `PATH` Windows ou cree un alias PowerShell.

Cette commande modifie `.env`:

- `ALGO_TRADING_MODE`;
- `TWS_ACCOUNT` avec le compte actif du mode choisi;
- `ALGO_INSTANCE_ID=V8_ES`;
- `IBKR_AUTO_START_GATEWAY=true`;
- `IBKR_HOST=host.docker.internal` si absent.

Elle ne stocke pas tes comptes fixes ni tes secrets dans `.env`: `TWS_ACCOUNT_PAPER`, `TWS_ACCOUNT_LIVE`, `TWS_USERNAME`, `TWS_PASSWORD` et `DATABENTO_API_KEY` doivent rester en variables utilisateur Windows.

Le fichier `.env` est cree dans le dossier du projet V8, a cote de `docker-compose.yml`, meme si tu lances `algoctl-vps` depuis un autre dossier via le `PATH`.

Puis elle verifie si l'image Docker existe deja.

Si l'image n'existe pas encore, elle build automatiquement une premiere fois:

```powershell
docker compose --env-file .env -f docker-compose.yml build strategy-prod-v8-es
```

Si l'image existe deja, elle saute le build.

Ensuite elle recree le container strategie:

```sh
docker compose --env-file .env -f docker-compose.yml up -d --force-recreate strategy-prod-v8-es
```

En pratique, tu n'as donc plus besoin de taper cette ligne toi-meme.

Important: `algoctl-vps` ne rebuild pas l'image au quotidien si elle existe deja. Si tu modifies le code Python, le Dockerfile ou les requirements, rebuild manuellement:

```powershell
docker compose --env-file .env -f docker-compose.yml build strategy-prod-v8-es
```

Puis relance:

```powershell
algoctl-vps PROD-ES PAPER
```

Dans les logs, verifier ensuite:

```text
IBKR_MODE_SELECTED | mode=PAPER
```

ou:

```text
IBKR_MODE_SELECTED | mode=LIVE
```

Important: `algoctl-vps` est une commande cote hote/VPS, pas une commande Docker Exec dans le container. Elle sert a preparer `.env`, puis a lancer/recreer le container.

### IB Gateway paper/live

Le container strategie se connecte a l'IB Gateway indique par:

```env
IBKR_HOST=host.docker.internal
```

Avec cette valeur, IB Gateway tourne hors du container strategie, sur le VPS/hote ou dans un autre container expose sur l'hote.

Par defaut, l'algo essaie maintenant de gerer automatiquement le gateway IBKR dockerise:

```env
IBKR_AUTO_START_GATEWAY=true
```

Au demarrage:

- en mode `paper`, il verifie le container `nautilus-ib-gateway-paper`;
- en mode `live`, il verifie le container `nautilus-ib-gateway-live`;
- si le container n'existe pas, il le cree;
- s'il existe mais est arrete ou casse, il le remplace;
- s'il tourne deja et que le login IBKR est pret, il le reutilise.

Le gateway est lance avec:

```env
READ_ONLY_API=no
```

via `read_only_api=False`, sinon l'algo ne pourrait pas envoyer d'ordres.

Pour que l'auto-start fonctionne depuis le container strategie, Docker doit etre accessible depuis ce container. Le compose monte donc:

```yaml
- /var/run/docker.sock:/var/run/docker.sock
```

C'est pratique, mais puissant: ce container peut alors parler au Docker du VPS. A garder uniquement pour un container de confiance.

Si tu preferes gerer IB Gateway toi-meme avec Docker Desktop, mets:

```env
IBKR_AUTO_START_GATEWAY=false
```

Dans ce cas, l'algo ne fait que se connecter au gateway deja lance.

### Client ID IBKR

Chaque connexion API IBKR doit avoir un `client_id` different. Sinon deux algos peuvent se marcher dessus.

Par defaut, V8 ES utilise une identite d'instance et une plage:

```env
IBKR_CLIENT_ID=        # optionnel
ALGO_INSTANCE_ID=V8_ES
```

`ALGO_INSTANCE_ID` sert a identifier cette instance d'algo dans le registre des `client_id` IBKR. Exemple: `V8_ES` et un futur `V8_NQ` peuvent partager le meme gateway IBKR sans prendre le meme `client_id`.

Si `IBKR_CLIENT_ID` est defini, l'algo utilise exactement cette valeur.

Si `IBKR_CLIENT_ID` est vide, l'algo reserve automatiquement un ID dans:

```text
/app/control/ibkr_client_ids.json
```

La plage commence a `11` et couvre 20 IDs. Pour un futur algo NQ, il faudra lui donner un autre `ALGO_INSTANCE_ID`, par exemple:

```env
ALGO_INSTANCE_ID=V8_NQ
```

ou mieux, une autre plage dans son fichier `config.py`.

Ce registre evite surtout les collisions entre tes propres algos. Il ne peut pas deviner a 100 % les connexions externes faites a la main depuis TWS, NinjaTrader ou un autre programme. Si besoin, force un ID avec:

```env
IBKR_CLIENT_ID=31
```

Apres un crash brutal, si un ancien ID reste reserve alors que l'algo ne tourne plus, tu peux ouvrir:

```text
control/ibkr_client_ids.json
```

et supprimer l'entree obsolete.

## Separation config / control

La config statique vit dans `config.py`: instrument, horaires, parametres V8, Databento, IBKR et limites globales.

Le fichier JSON `control/v8_es.json` reste volontairement separe, car il sert de panneau de controle a chaud depuis Docker Exec.

## Envoi simple sur le VPS

### Option recommandee: copier le dossier et build sur le VPS

C'est le plus simple au debut, parce que tu vois clairement les fichiers et tu peux modifier `control/v8_es.json` ou `.env` directement sur le VPS.

1. Copier ce dossier sur le VPS:

```text
python_prod/strategy_prod_v8
```

Tu peux le faire par copier/coller RDP, zip, OneDrive, ou `scp`.

2. Definir les variables utilisateur Windows stables. Le fichier `.env` sera cree ou mis a jour automatiquement par `algoctl-vps`.

Variables utilisateur Windows recommandees, depuis PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("DATABENTO_API_KEY", "ta_cle_databento", "User")
[Environment]::SetEnvironmentVariable("TWS_USERNAME", "ton_login_ibkr", "User")
[Environment]::SetEnvironmentVariable("TWS_PASSWORD", "ton_mdp_ibkr", "User")
[Environment]::SetEnvironmentVariable("TWS_ACCOUNT_PAPER", "ton_compte_paper", "User")
[Environment]::SetEnvironmentVariable("TWS_ACCOUNT_LIVE", "ton_compte_live", "User")
```

Variables optionnelles:

```powershell
[Environment]::SetEnvironmentVariable("IBKR_ES_INSTRUMENT_ID", "ESU6.CME", "User")
[Environment]::SetEnvironmentVariable("IBKR_HOST", "host.docker.internal", "User")
```

`IBKR_ES_INSTRUMENT_ID` est optionnel. Si tu ne le mets pas, le code calcule le front-month ES automatiquement avec une regle calendrier.

La config contient `databento_symbol="AUTO_FROM_EXECUTION"`. Cela veut dire: utiliser le meme contrat concret que l'execution IBKR, mais avec le venue Databento. Exemple:

```text
execution IBKR : ESU6.CME
data Databento : ESU6.GLBX
```

Le rollover automatique se fait au demarrage avec `ibkr_rollover_days_before_expiry=8`. Si l'algo reste allume pendant que la date de rollover passe, il ne change pas de contrat a chaud. Il bloque les nouvelles entrees et logge:

```text
ROLLOVER_RESTART_REQUIRED
```

Dans ce cas, il faut verifier que la position est flat, puis redemarrer le container pour repartir sur le nouveau contrat.

`IBKR_HOST` est optionnel. Dans Docker Desktop, `host.docker.internal` permet au container de joindre IB Gateway qui tourne sur le VPS/hote.

Apres ca, fermer puis rouvrir PowerShell ou Docker Desktop pour que les nouvelles variables soient visibles par `docker compose`.

Pour verifier cote Windows:

```powershell
if ($env:DATABENTO_API_KEY) { "DATABENTO_API_KEY=present" } else { "DATABENTO_API_KEY=missing" }
if ($env:TWS_USERNAME) { "TWS_USERNAME=present" } else { "TWS_USERNAME=missing" }
if ($env:TWS_PASSWORD) { "TWS_PASSWORD=present" } else { "TWS_PASSWORD=missing" }
echo $env:TWS_ACCOUNT_PAPER
echo $env:TWS_ACCOUNT_LIVE
echo $env:IBKR_ES_INSTRUMENT_ID
echo $env:IBKR_HOST
```

Pour verifier cote container une fois lance, eviter d'afficher la cle Databento en clair. Verifie seulement sa presence:

```sh
docker exec -it strategy-prod-v8-es sh -lc 'test -n "$DATABENTO_API_KEY" && echo DATABENTO_API_KEY=present'
docker exec -it strategy-prod-v8-es sh -lc 'test -n "$TWS_USERNAME" && echo TWS_USERNAME=present'
docker exec -it strategy-prod-v8-es sh -lc 'test -n "$TWS_PASSWORD" && echo TWS_PASSWORD=present'
docker exec -it strategy-prod-v8-es printenv ALGO_TRADING_MODE
docker exec -it strategy-prod-v8-es printenv TWS_ACCOUNT
docker exec -it strategy-prod-v8-es printenv TWS_ACCOUNT_PAPER
docker exec -it strategy-prod-v8-es printenv TWS_ACCOUNT_LIVE
docker exec -it strategy-prod-v8-es printenv IBKR_ES_INSTRUMENT_ID
docker exec -it strategy-prod-v8-es printenv IBKR_HOST
```

Eviter aussi de lancer ou partager:

```powershell
docker compose config
```

Cette commande est utile pour debugger Docker, mais elle affiche les variables d'environnement resolues, donc elle peut montrer des secrets en clair dans le terminal.

3. Lancer l'algo:

```sh
algoctl-vps PROD-ES PAPER
```

Au premier lancement, si l'image `strategy-prod-v8-es:local` n'existe pas encore, `algoctl-vps` la build automatiquement.

4. Si tu modifies le code plus tard, rebuild manuellement avant de relancer:

```sh
docker compose --env-file .env -f docker-compose.yml build strategy-prod-v8-es
```

5. Voir les logs:

```sh
docker logs -f strategy-prod-v8-es
```

6. Controler l'algo:

```sh
docker exec -it strategy-prod-v8-es algoctl status
docker exec -it strategy-prod-v8-es algoctl pause
docker exec -it strategy-prod-v8-es algoctl resume
```

### Option image Docker: build local puis envoyer un `.tar`

Utile si tu veux envoyer exactement la meme image au VPS, sans rebuild la-bas.

Sur ton PC:

```sh
docker build -t strategy-prod-v8-es:latest .
docker save strategy-prod-v8-es:latest -o strategy-prod-v8-es.tar
```

Copier ensuite `strategy-prod-v8-es.tar` sur le VPS.

Sur le VPS:

```sh
docker load -i strategy-prod-v8-es.tar
docker compose --env-file .env -f docker-compose.yml up -d
```

Avec cette option, il faut quand meme copier sur le VPS:

- `docker-compose.yml`
- `.env`
- `control/v8_es.json`

Les secrets comme `DATABENTO_API_KEY`, `TWS_USERNAME` et `TWS_PASSWORD` ne doivent jamais etre mis dans l'image Docker. Dans l'architecture recommandee, ils restent en variables utilisateur Windows. Le fichier `.env` garde seulement l'etat actif de lancement.

## Statut

Cette base compile cote Python et pose la logique V8.

Le cycle live est maintenant:

1. signal V8 accepte;
2. envoi d'un ordre market IBKR;
3. etat `ENTRY_PENDING`;
4. passage en position uniquement apres `OrderFilled`;
5. calcul TP/SL depuis le prix moyen reel;
6. envoi TP limit + SL stop-market avec tags OCA IBKR;
7. modification du stop vers lock +1 apres le trigger;
8. retour flat sur fill TP/SL ou `PositionClosed`.

Par defaut, `AUTO_FROM_EXECUTION` indique que le symbole Databento est derive du contrat IBKR concret. Au lancement, le code resout un contrat concret, puis utilise ce contrat concret cote Databento et cote IBKR, par exemple `ESU6.GLBX` pour les donnees et `ESU6.CME` pour l'execution. Pour forcer un contrat, definir `IBKR_ES_INSTRUMENT_ID`, par exemple `ESU6.CME`.

Avant argent reel, faire un test paper pour verifier les noms exacts des events IBKR/Nautilus dans les logs `ORDER_FILLED`, `BRACKET_SUBMITTED`, `POSITION_FLAT`.
