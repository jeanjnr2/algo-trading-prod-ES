# Strategy Prod V8

Repo du moteur de signal V8.

Python lit Databento, reconstruit les bougies 10s, calcule le signal V8, puis publie uniquement une intention `LONG` ou `SHORT` via ZeroMQ.

Python ne passe aucun ordre broker. L'execution reste geree cote NinjaTrader.

## Architecture

```text
Databento live
  -> Docker / Python / Nautilus / moteur V8
  -> ZeroMQ SIGNAL LONG/SHORT
  -> NinjaTrader, via bridge local hors repo
  -> execution NinjaTrader
```

Flux ZeroMQ:

```text
Python PUB  tcp://*:5555  -> Ninja SUB  tcp://127.0.0.1:5555
Ninja PUSH  tcp://127.0.0.1:5556 -> Python PULL tcp://*:5556
```

Python envoie:

```json
{
  "type": "SIGNAL",
  "signal_id": "V8_ES-260715143510-ab12cd34",
  "algo": "V8_ES",
  "side": "LONG",
  "timestamp_utc": "2026-07-15T14:35:10.000000+00:00",
  "expires_at_utc": "2026-07-15T14:35:12.000000+00:00"
}
```

Ninja repond:

```json
{"type":"READY","signal_id":"","reason":"ninja_zmq_started"}
{"type":"HEARTBEAT","signal_id":"","reason":"ninja_alive"}
{"type":"ACK","signal_id":"...","reason":"accepted_by_ninja"}
{"type":"REJECTED","signal_id":"...","reason":"spread_too_wide"}
{"type":"POSITION_OPEN","signal_id":"...","reason":"entry_filled"}
{"type":"POSITION_FLAT","signal_id":"...","reason":"flat"}
```

Au demarrage ou a la reconnexion de `TheBridge`, Ninja renvoie aussi un snapshot:

- `POSITION_FLAT` si le compte/chart est flat;
- `POSITION_OPEN` si une position existe deja sur l'instrument du chart.

Pendant `POSITION_OPEN`, Python continue de lire Databento et de calculer la V8, mais il n'envoie plus de nouveau signal. Apres `POSITION_FLAT`, Python peut renvoyer un signal.

## Fichiers importants

```text
src/v8_engine.py      moteur V8 pur
src/bar_builder.py    construction des bougies 10s
src/strategy.py       wrapper Nautilus signal-only
src/bridge.py         bridge ZeroMQ cote Python
src/run_live.py       demarrage Nautilus + Databento
TheBridge.cs          bridge NinjaTrader a compiler dans Ninja, hors Docker
docker-compose.yml    lancement du moteur Python
Dockerfile            image Docker Python
requirements.txt      dependances Python
```

`TheBridge.cs` reste dans le repo pour versionner le code NinjaTrader, mais il est exclu de l'image Docker via `.dockerignore`.

## Lancement Docker

Variable d'environnement obligatoire sur le VPS:

```text
DATABENTO_API_KEY
```

Build puis lancement:

```powershell
docker compose up -d --build strategy-prod-v8-es
```

Relancer sans rebuild si le code n'a pas change:

```powershell
docker compose up -d strategy-prod-v8-es
```

Voir les logs:

```powershell
docker logs -f strategy-prod-v8-es
```

Arreter le moteur Python:

```powershell
docker compose stop strategy-prod-v8-es
```

## Comportement Backend

Au demarrage, Python:

1. charge la config objet dans `src/config.py`;
2. resout le contrat Databento concret;
3. demarre le bridge ZeroMQ;
4. s'abonne aux trades et quotes Databento;
5. attend `READY` ou `HEARTBEAT` de Ninja avant d'autoriser l'envoi de signaux.

Logs attendus cote Python:

```text
NINJA_BRIDGE_STARTED
NINJA_READY
NINJA_LINK_READY
MARKET_DATA_SUBSCRIBED
STRATEGY_STARTED
```

Si le lien Ninja tombe:

```text
NINJA_CONNECTION_LOST
NINJA_RECONNECTED
```

Tant que Ninja n'est pas pret, Python calcule le marche mais ne publie aucun signal.

## Contrat et Rollover

Le contrat execute est celui du chart NinjaTrader.

Python resout seulement le contrat Databento utilise pour la data, par exemple `ESU6.GLBX`. Cette resolution sert uniquement au flux de donnees Python.

Au rollover:

1. passer le chart Ninja sur le nouveau contrat;
2. verifier que le bridge Ninja est actif sur ce chart;
3. relancer le container Python si le symbole Databento doit changer.

## Mise En Prod

Checklist simple:

1. Docker Desktop est lance sur le VPS.
2. `DATABENTO_API_KEY` existe en variable d'environnement.
3. Le container Python tourne.
4. Les logs Python montrent `NINJA_BRIDGE_STARTED`.
5. NinjaTrader est connecte au broker.
6. Le bon chart ES est ouvert.
7. Le bridge Ninja local est actif sur ce chart.
8. Les ports sont coherents:

```text
SignalPort 5555
StatePort  5556
```

9. Le compte, la quantite, le spread, TP/SL et les limites de risque sont regles cote Ninja.

## Notes

- Python ne connait pas le prix de fill.
- Python ne connait pas le compte Ninja.
- Python ne gere pas la marge.
- Python ne ferme pas les positions.
- NinjaTrader reste responsable de l'execution.
- Les signaux expirent vite pour eviter une execution tardive.
