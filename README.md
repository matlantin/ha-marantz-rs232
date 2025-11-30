Guide d'installation pour l'intégration Marantz RS232
===================================================

Installation
------------

1. Copier le dossier `marantz_rs232` dans `config/custom_components/` de Home Assistant.
   - Exemple sur Windows : `C:\Users\<user>\AppData\Roaming\.homeassistant\custom_components\`
2. Installer la dépendance Python `pyserial` si nécessaire (généralement gérée par Home Assistant si listée dans `manifest.json`).
3. Ajouter dans `configuration.yaml` la configuration du platform `media_player` :

```yaml
media_player:
  - platform: marantz_rs232
    name: "Marantz SR6001"
    serial_port: "/dev/ttyUSB0"   # ou COM3 sur Windows
    baudrate: 9600
    command_map:
      # Exemples (placeholders) : remplacez par les commandes exactes du SR6001
      power_on: "PWON"
      power_off: "PWSTANDBY"
      volume_up: "MVUP"
      volume_down: "MVDOWN"
      mute_on: "MUTON"
      mute_off: "MUTOFF"
      select_source: "SI{source}"  # template : {source} sera remplacé
      query_power: "PW?"
      query_volume: "MV?"
      query_source: "SI?"
```

4. Redémarrer Home Assistant.

Remplir `command_map`
---------------------

Le code fourni est un scaffold : il envoie les chaînes présentes dans `command_map`
vers le port série. Vous devez fournir la correspondance exacte des commandes RS232
du Marantz SR6001 (fournie dans votre documentation PDF). Si vous voulez, je
peux extraire automatiquement la table de commandes depuis le PDF joint et remplir
les valeurs ici — dites-moi si vous m'autorisez à le faire.

Tests
-----

- Dans Outils de développement -> Services, vous pouvez appeler `media_player.turn_on`
  (target = votre entité) pour tester l'envoi de la commande `power_on`.
- Vous pouvez aussi utiliser la fonction `query_*` définie dans `command_map` pour
  récupérer des états, qui seront loggés. Une fois la syntaxe de réponse connue,
  nous adapterons le parsing pour alimenter correctement `state`, `volume_level` et `source`.

Support et itération
--------------------

Je peux :
- Extraire les commandes RS232 du PDF (si vous me donnez l'autorisation).
- Adapter le parsing des réponses (une fois que vous m'indiquerez leur format clair).
- Ajouter des services personnalisés pour envoyer des commandes brutes depuis Home Assistant.

Mapping extrait automatiquement
------------------------------

J'ai extrait la table des commandes du PDF et créé un exemple de `command_map`
prêt à l'emploi dans `command_map_parsed.yaml` (dans le dossier du composant).
Ce fichier contient les commandes courantes détectées pour :

- Power (on/off/toggle)
- Volume (up/down et set absolu)
- Mute (audio/video)
- Sélection des sources courantes (TV, DVD, AUX1, HDMI1, etc.)
- Commandes de requête (ex: `@PWR:?`)

Important : les commandes brutes doivent être envoyées précédées du caractère de
démarrage `@` et terminées par CR (retour chariot, 0x0D). Vérifiez les résultats
avec les logs et ajustez si nécessaire.

Si vous voulez, j'intègre ce mapping directement dans `media_player.py` comme
valeur par défaut ou je peux le laisser séparé pour que vous le modifiiez plus
facilement.

Service de debug `marantz_rs232.send_raw`
----------------------------------------

Un service `marantz_rs232.send_raw` a été ajouté pour envoyer une commande RS232
arbitraire depuis Home Assistant. Exemple d'utilisation depuis Developer Tools → Services :

Service: `marantz_rs232.send_raw`

Service Data (YAML) :

```yaml
command: "@PWR:2"
entity_id: media_player.marantz_sr6001  # optionnel, cible l'entité
```

Ce service est utile pour tester des commandes sans modifier `configuration.yaml` ou
redémarrer Home Assistant.

Lecture de la réponse et écoute d'évènement
----------------------------------------

Le service lit maintenant la réponse renvoyée par l'ampli et publie un évènement
`marantz_rs232_raw_response` sur le bus d'évènements Home Assistant. Pour écouter
cet évènement (Developer Tools → Events) utilisez :

Event to subscribe: `marantz_rs232_raw_response`

Le payload de l'évènement contient :

```json
{
  "entity_id": "media_player.marantz_sr6001",
  "command": "@PWR:2",
  "response": "... la réponse brute en texte ..."
}
```

Utilisez cet évènement pour capturer les réponses et me les fournir si vous
voulez que j'améliore le parsing automatique.

Optimistic updates et mapping automatique
----------------------------------------

Options de configuration :

- `poll_interval` (int) : intervalle en secondes pour interroger périodiquement l'ampli (par défaut 10).
- `optimistic` (bool) : si `true`, l'entité mettra à jour son état immédiatement après l'envoi d'une commande (utile si l'ampli ne répond pas toujours).
- `use_marantzusb_format` (bool) : si `true`, le composant enverra les commandes absolues de volume au format Marantz-USB (ex. `@VOL:0-23`) — ce format est plus rapide que l'envoi de pas et est activé par défaut.

Exemple de configuration :

```yaml
media_player:
  - platform: marantz_rs232
    name: "Marantz SR6001"
    serial_port: "/dev/ttyUSB0"
    baudrate: 9600
    poll_interval: 10
    optimistic: true
    use_marantzusb_format: true
```


Mapping automatique des sources inconnues

- Lorsque l'intégration reçoit un code source inconnu (par ex. `@SRC:33`), elle crée automatiquement
  une entrée dans `command_map_parsed.yaml` (clé `sources`) du composant, par exemple :

```yaml
sources:
  SRC_33: "SRC:33"
```

  Ceci permet d'afficher immédiatement le code reçu comme source dans Home Assistant et de
  modifier ultérieurement la clé `SRC_33` pour donner un nom lisible (par ex. `HDMI3`). L'écriture
  sur le fichier est best-effort ; si votre installation n'autorise pas l'écriture, la création
  de la clé échouera et un message d'erreur sera loggé.
