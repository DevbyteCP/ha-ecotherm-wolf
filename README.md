# EcoTherm Wolf

**Deine Wolf-Heizung direkt in Home Assistant.**

> **⚠️ Wichtig: Ohne vier Wolf-Dateien funktioniert die Integration nicht.**
> `device.xml`, `parameter.xml`, `converter.xml` und `client.pfx` sind **nicht**
> in diesem Repository enthalten. Sie gehören in den Ordner **`/config/ecotherm_wolf/`**
> und lassen sich im Terminal von Home Assistant mit zwei Befehlen holen:
>
> ```sh
> wget -O /tmp/fetch_wolf_files.py https://raw.githubusercontent.com/DevbyteCP/ha-ecotherm-wolf/main/scripts/fetch_wolf_files.py
> python3 /tmp/fetch_wolf_files.py /config/ecotherm_wolf
> ```
>
> Weitere Wege (ohne Python, von Hand) unter [Wolf-Dateien bereitstellen](#wolf-dateien-bereitstellen).

EcoTherm Wolf verbindet Home Assistant lokal mit einem **Wolf-ISM7-Gateway**.
Du kannst Temperaturen, Betriebszustände und Verbrauchszähler anzeigen sowie
unterstützte Sollwerte und Betriebsarten einstellen. Ein MQTT-Broker ist nicht nötig.

## Funktionen

- Heizungsgeräte und unterstützte Untermodule automatisch erkennen.
- Wichtige Werte übersichtlich als Home-Assistant-Entitäten bereitstellen.
- Zwischen einer kleinen, üblichen oder vollständigen Parameterauswahl wählen.
- Verbindungen automatisch wiederherstellen und längere Ausfälle melden.

Erprobt mit einer Wolf-CHA-Wärmepumpe sowie BM-2-, MM- und CWL-Modulen.
Welche Werte und Einstellungen verfügbar sind, hängt von deiner Anlage ab.
Das Projekt ist weiterhin als **Beta** gekennzeichnet.

## Voraussetzungen

- Home Assistant und ein im Netzwerk erreichbares Wolf-ISM7-Gateway.
- IP-Adresse und Gateway-Passwort; der übliche Verbindungsport ist `9092`.
- Keine gleichzeitig laufende andere Verbindung zum ISM7, etwa über eine
  weitere Integration: Das Gateway erlaubt nur eine solche Verbindung zur Zeit.

## Installation über HACS

1. In HACS oben rechts **⋮ → Benutzerdefinierte Repositories** öffnen.
2. `https://github.com/DevbyteCP/ha-ecotherm-wolf` eintragen, Typ **Integration**.
3. **EcoTherm Wolf** herunterladen und Home Assistant neu starten.

Neue Versionen meldet HACS danach automatisch als Update.
Eine Aufnahme in den HACS-Standardkatalog ist noch nicht erfolgt.

Ohne HACS: `custom_components/ecotherm_wolf` in den Ordner
`<config>/custom_components/` kopieren und Home Assistant neu starten.

## Wolf-Dateien bereitstellen

Die Integration braucht vier Dateien von Wolf, die **nicht** in diesem
Repository liegen: `device.xml`, `parameter.xml`, `converter.xml` (Geräte- und
Parameterbeschreibungen) und `client.pfx` (Zertifikat für die ISM7-Verbindung).
Sie stammen aus dem Projekt [zivillian/ism7mqtt](https://github.com/zivillian/ism7mqtt).

Die Dateien gehören in den Ordner **`<config>/ecotherm_wolf/`**. Der liegt
außerhalb von `custom_components`, deshalb bleiben sie bei Updates erhalten.

Am einfachsten im Terminal von Home Assistant:

```sh
wget -O /tmp/fetch_wolf_files.py https://raw.githubusercontent.com/DevbyteCP/ha-ecotherm-wolf/main/scripts/fetch_wolf_files.py
python3 /tmp/fetch_wolf_files.py /config/ecotherm_wolf
```

Das Skript lädt genau die getestete Fassung und prüft jede Datei per SHA-256.
Ohne `python3` geht es auch nur mit `curl`, dann allerdings ohne Prüfsummen:

```sh
mkdir -p /config/ecotherm_wolf && cd /config/ecotherm_wolf
U=https://raw.githubusercontent.com/zivillian/ism7mqtt/3aa40b1ad28eff8c112ad6a0d17c21800a7476b9/src
curl -fLO $U/ism7mqtt/Resources/device.xml
curl -fLO $U/ism7mqtt/Resources/parameter.xml
curl -fLO $U/ism7mqtt/Resources/converter.xml
curl -fLO $U/ism7ssl/Resources/client.pfx
```

Hat man die Dateien schon woanders (z. B. aus einer Kopie von ism7mqtt), prüft und kopiert sie
`python3 /tmp/fetch_wolf_files.py /config/ecotherm_wolf --from <ordner>`.
Alternativ die Dateien von Hand herunterladen (Pfade und Prüfsummen stehen in
[`scripts/fetch_wolf_files.py`](scripts/fetch_wolf_files.py)) und in den Ordner kopieren.

Fehlen Dateien, zeigt die Einrichtung, welche, und unter **Einstellungen →
Reparaturen** erscheint ein Hinweis. Sobald die Dateien da sind, startet die
Integration von selbst.

## Einrichtung

1. Unter **Einstellungen → Geräte & Dienste → Integration hinzufügen** nach
   **EcoTherm Wolf** suchen.
2. Gateway-Adresse, Port und Passwort eingeben.
3. Die erkannten Geräte auswählen und mit der Parameterauswahl **standard** beginnen.
   Bei nicht erkannten Geräten ist eine manuelle Einrichtung möglich.

## Gut zu wissen

Bei Verbindungsproblemen können Messwerte den letzten bekannten Stand behalten.
Beachte deshalb auch die Diagnosewerte zur Verbindung. Ein automatischer
Gateway-Neustart ist optional und muss ausdrücklich eingerichtet werden.

Bei Problemen helfen die Diagnoseinformationen der Integration und eine Meldung
unter [GitHub Issues](https://github.com/DevbyteCP/ha-ecotherm-wolf/issues).
Bitte Diagnoseexports vor dem Teilen auf private Angaben prüfen.

EcoTherm Wolf · von **DevbyteCP**

Die Protokollanbindung basiert auf
[zivillian/ism7mqtt](https://github.com/zivillian/ism7mqtt), ebenfalls unter GPL-3.0.
Die Wolf-Dateien sind nicht Teil dieses Repositorys; die Rechte daran liegen bei
ihren jeweiligen Inhabern.

## Lizenz

Copyright (C) 2026 DevbyteCP. Die eigenen Projektbestandteile stehen unter der
[GNU GPL, Version 3 oder neuer](LICENSE) (`GPL-3.0-or-later`). Nutzen, Verändern
und Weitergeben – auch gegen Bezahlung – sind unter den Lizenzbedingungen erlaubt.
Bei Weitergabe abgeleiteter Programme müssen die GPL und ihre Pflichten zur
Bereitstellung des zugehörigen Quellcodes eingehalten werden.
Gesonderte Urheber- und Lizenzhinweise für Bestandteile Dritter bleiben erhalten.
