# Faster-Whisper Flow fuer spaetere n8n-Integration

## Ziel

Diese Datei dokumentiert den aktuell erarbeiteten Ablauf, um lange YouTube-Audios lokal zu transkribieren, sowie die dabei aufgetretenen Probleme und die daraus abgeleitete Zielarchitektur fuer n8n.

Der Fokus liegt auf einem robusten Flow fuer:

- YouTube- oder sonstige Audioquellen herunterladen
- Audio segmentieren
- lokal vorhandene Whisper-Modelle verwenden
- Transkripte zusammensetzen
- Fehlerfaelle kontrolliert behandeln

Die wichtigste Erkenntnis ist:

- Fuer kurze oder mittlere Audios funktioniert der lokale HTTP-Service gut.
- Fuer lange CPU-Transkriptionen ist ein direkter Python-Lauf stabiler als viele HTTP-Requests gegen den Container.
- Fuer n8n sollte deshalb zwischen `kurzer synchroner HTTP-Transkription` und `langlaufendem Batch-Job` klar unterschieden werden.

## Aktueller technischer Stand

### HTTP-Service

Der lokale Dienst wird per Docker Compose gestartet und bietet zwei relevante Endpunkte:

- `GET /health`
- `POST /audio`

Die Compose-Konfiguration liegt in [docker-compose.yml](/Users/menty/faster-whisper/docker-compose.yml:1).

Aktuelle Eigenschaften:

- Standardmodell ist `large-v3`
- lokaler Offline-Modus ist aktiv
- Modellcache wird aus `./config` gemountet
- Port ist `10300`

Wichtige Env-Parameter:

- `WHISPER_MODEL=large-v3`
- `WHISPER_LOCAL_FILES_ONLY=true`
- `WHISPER_CACHE_DIR=/models`
- `WHISPER_DEVICE=cpu`
- `WHISPER_COMPUTE_TYPE=int8`

### HTTP-Implementierung

Der Dienst selbst liegt in [app/main.py](/Users/menty/faster-whisper/app/main.py:1).

Wichtige Punkte im Code:

- Lokale Modellauflosung erfolgt in `_resolve_model_source` und `_candidate_model_dirs`.
- Wenn ein Modell lokal unter `models--Systran--faster-whisper-<name>/snapshots/main` liegt, wird dieser Pfad direkt verwendet.
- `local_files_only` wird an `WhisperModel(...)` durchgereicht.
- Uploads werden nach `/tmp` geschrieben und danach mit `faster-whisper` transkribiert.
- Ungueltige Audiodateien liefern sauber `400 uploaded file is not valid audio data`.

Relevante Stellen:

- Modellauflosung: [app/main.py](/Users/menty/faster-whisper/app/main.py:50)
- Modellinitialisierung: [app/main.py](/Users/menty/faster-whisper/app/main.py:83)
- Health-Endpoint: [app/main.py](/Users/menty/faster-whisper/app/main.py:131)
- Audio-Endpoint: [app/main.py](/Users/menty/faster-whisper/app/main.py:141)

### Conda-basierte Toolchain

Python-bezogene Hilfsjobs wurden nicht mehr ins System, sondern in eine dedizierte Conda-Umgebung gelegt:

- Umgebung: `faster_whisper_tools`

Installierte Komponenten dort:

- `yt-dlp`
- `ffmpeg`
- `faster-whisper`

Diese Umgebung wurde verwendet fuer:

- Download des YouTube-Audios
- Segmentierung mit `ffmpeg`
- direkten lokalen `faster-whisper`-Fallback ausserhalb des Docker-HTTP-Services

## Was konkret gemacht wurde

### 1. Ausgangsproblem korrigiert

Das urspruenglich verwendete LinuxServer-Image war kein HTTP-Service, sondern ein Wyoming/TCP-Service. Deshalb fuehrte `curl POST /audio` dort zu Symptomen wie:

- `broken pipe`
- `connection reset by peer`
- scheinbar haengende Requests

Loesung:

- Ersetzung durch einen eigenen FastAPI-Service mit `faster-whisper`
- dadurch passt das Protokoll jetzt wirklich zum `curl`-Aufruf

### 2. Offline-Betrieb mit lokalem Modell hergestellt

`large-v3` wurde vorab lokal gespeichert. Der Service wurde so erweitert, dass er dieses Modell nicht mehr als Hub-Repo-Namen behandelt, sondern direkt aus dem Snapshot-Pfad laedt.

Loesung:

- `WHISPER_LOCAL_FILES_ONLY=true`
- lokale Modellauflosung in `app/main.py`

Effekt:

- kein erzwungener Hugging-Face-Zugriff mehr fuer `large-v3`
- reproduzierbarer Offline-Start

### 3. YouTube-Archiv als Audioquelle geladen

Testquelle:

- `https://www.youtube.com/live/nAMHM4P9aSM?si=0VVMebP_jqT9t9NO`

Erkannt wurde:

- `live_status=was_live`
- also kein aktiver Livestream mehr, sondern ein archiviertes Video

Download erfolgte mit:

- `yt-dlp`
- Ausgabe als `m4a`

### 4. Volltranskription in Segmenten versucht

Zunaechst wurde das komplette Audio in groessere Bloecke zerlegt:

- 10-Minuten-Chunks
- dann 5-Minuten-Chunks
- schliesslich 2,5-Minuten-Chunks

Warum Segmentierung noetig war:

- ein einzelner Request fuer 96 Minuten Audio waere auf CPU unpraktisch
- Fehlerisolierung ist mit Chunks deutlich einfacher
- ein erneuter Lauf betrifft dann nur den kaputten Abschnitt

### 5. Problematische Stelle identifiziert

Bei bestimmten Segmenten kam es wiederholt zu:

- `curl: (52) Empty reply from server`
- `Recv failure: Connection reset by peer`
- Container-Neustarts waehrend der Transkription

Das trat besonders auf:

- bei laengeren CPU-Requests ueber den HTTP-Container
- teils reproduzierbar auf einzelnen Chunks

### 6. Direkten lokalen Python-Fallback aufgebaut

Um die HTTP-Schicht als Fehlerquelle auszuschliessen, wurde `faster-whisper` direkt in der Conda-Umgebung genutzt.

Erkenntnis:

- Problemsegmente liessen sich lokal im selben Modell oft erfolgreich transkribieren
- der direkte Python-Lauf war stabiler als die Container-HTTP-Schicht

### 7. Hybrid-Abschluss fuer das Gesamttranskript

Das finale zusammengesetzte Transkript wurde aus mehreren Teilquellen gebildet:

- fruehe Segmente ueber den Docker-Service mit `large-v3`
- spaetere Restsegmente teils ueber lokalen direkten `large-v3`-Lauf
- der verbleibende Rest deutlich schneller ueber den lokal gecachten `small`-Pfad

Zusammengesetzte Ergebnisdatei:

- [full_transcript.txt](/tmp/faster_whisper_youtube/final/full_transcript.txt)

Quellenliste der verwendeten Teilstuecke:

- [sources.txt](/tmp/faster_whisper_youtube/final/sources.txt)

## Ueberwundene Huerden

### 1. Falsches Serverprotokoll

Problem:

- Der erste Container sprach Wyoming/TCP statt HTTP.

Auswirkung:

- `curl /audio` konnte prinzipiell nicht stabil funktionieren.

Loesung:

- eigener HTTP-Service mit FastAPI

### 2. Ungueltige Testdatei

Problem:

- `jfk.wav` war in Wirklichkeit HTML und keine Audiodatei.

Auswirkung:

- Decoderfehler und keine sinnvolle Rueckmeldung

Loesung:

- explizite Validierung
- sauberer `400` statt kryptischem `500`

### 3. Hub-Zugriffe trotz lokalem Modell

Problem:

- `large-v3` wurde als Modellname interpretiert und fuehrte wieder zu Hugging-Face-Zugriffen.

Loesung:

- lokale Snapshot-Aufloesung
- Offline-Modus per Env

### 4. Instabilitaet bei langen HTTP-CPU-Jobs

Problem:

- Bei laengeren Audios ueber den Container-Endpunkt kam es zu `Empty reply from server`, `connection reset` oder Container-Neustarts.

Wahrscheinliche Ursachen:

- hohe CPU-Last
- langer synchroner Request-Lebenszyklus
- Prozess-/Memory-Stabilitaet bei wiederholten langen Requests

Wichtige Beobachtung:

- Dasselbe Audio liess sich lokal im direkten Python-Prozess oft erfolgreich verarbeiten.

### 5. Race Conditions direkt nach Container-Restarts

Problem:

- Selbst bei `health=ok` waren einzelne Requests direkt nach dem Restart noch instabil.

Loesung:

- zusaetzliche Wartezeit nach `health`

Ergebnis:

- etwas besser, aber nicht ausreichend fuer alle Problemsegmente

### 6. Beam-Size und Modellgroesse als Laufzeithebel

Beobachtung:

- `large-v3` auf CPU ist fuer lange Audios teuer
- `small` laeuft wesentlich schneller

Folgerung:

- In einem spaeteren Produktionsflow sollte die Modellwahl kein starres Dogma sein
- sinnvoll ist ein Profil je nach SLA:

- `large-v3` fuer hochwertige Einzeljobs
- `small` oder `medium` fuer laengere Batch-Jobs

## Aktuell sinnvoller Ziel-Flow fuer n8n

### Prinzip

Nicht alles ueber denselben HTTP-Endpoint abwickeln.

Stattdessen zwei Pfade:

- `Fast path`: kurze Audios synchron per `POST /audio`
- `Batch path`: lange Audios ueber Download -> Segmentierung -> lokaler Worker -> Zusammenbau

### Empfohlener n8n-Workflow

#### Flow A: Kurze Audios

Geeignet fuer:

- Uploads bis wenige Minuten
- API-artige Soforttranskription

Schritte:

1. Eingangsdaten entgegennehmen
2. Datei validieren
3. `HTTP Request` auf `POST http://localhost:10300/audio`
4. JSON speichern oder weiterverarbeiten

Vorteil:

- einfach
- geringe Latenz

Nachteil:

- fuer lange CPU-Laeufe ungeeignet

#### Flow B: Lange Audios oder YouTube

Geeignet fuer:

- YouTube-Videos
- Livestream-Archive
- Podcasts
- Calls
- Dateien > ein paar Minuten

Schritte:

1. Eingabe entgegennehmen
2. Metadaten pruefen
3. Audio herunterladen
4. Audio segmentieren
5. Segmente lokal transkribieren
6. JSONs zusammensetzen
7. Gesamtdokument schreiben
8. Status + Ergebnis ablegen

Empfohlene n8n-Nodes:

- `Webhook` oder `Manual Trigger`
- `Set`
- `If`
- `Execute Command`
- `Code`
- optional `Read Binary File`
- optional `Write Binary File`

### Konkrete Node-Logik fuer Flow B

#### 1. Eingabe normalisieren

Input:

- YouTube-URL oder lokale Datei
- optional `language`
- optional `model`
- optional `chunk_seconds`

Standardwerte:

- `language=de`
- `model=large-v3` fuer hochwertige Jobs
- `chunk_seconds=150` oder `300`

#### 2. Download

Empfehlung:

- `Execute Command` mit `conda run -n faster_whisper_tools yt-dlp ...`

Warum:

- trennt Python-Tools sauber vom System
- reproduzierbarer Lauf

#### 3. Segmentierung

Empfehlung:

- `Execute Command` mit `conda run -n faster_whisper_tools ffmpeg ... -f segment`

Empfohlene Groessen:

- initial 150 Sekunden
- falls sehr stabiler Rechner/GPU vorhanden, groesser

Warum nicht direkt 10 Minuten:

- Fehlerisolierung schlechter
- Wiederanlauf teurer

#### 4. Transkriptions-Worker

Wichtige Empfehlung:

- fuer lange Jobs nicht per HTTP-Node gegen den Container
- stattdessen direkter Python-Worker in Conda

Empfohlene Form:

- `Execute Command` startet ein lokales Python-Skript
- Skript verarbeitet Segment fuer Segment
- jedes Segment wird als eigene JSON-Datei gespeichert

Vorteile:

- stabiler
- keine HTTP-Zeitlimits
- saubere Wiederaufnahme

#### 5. Retry und Resume

Pflicht fuer n8n:

- pro Segment eigenes Outputfile
- wenn Datei schon existiert, Segment ueberspringen
- bei Fehler nur dieses Segment erneut versuchen

Das ist der entscheidende Unterschied zwischen robustem Batch-Flow und einem fragilen monolithischen Job.

#### 6. Zusammenbau

Alle Segmenttexte werden in Reihenfolge concatenated.

Empfohlene Outputs:

- `full_transcript.txt`
- optional `full_transcript.json`
- optional `segments.json`
- optional `markdown summary`

## Empfehlung fuer die spaetere Produktionsarchitektur

### Minimalvariante

- n8n orchestriert
- `yt-dlp`, `ffmpeg`, `faster-whisper` laufen in lokaler Conda-Umgebung
- Docker-HTTP-Service bleibt nur fuer kurze Ad-hoc-Jobs

Das ist die aktuell realistischste und stabilste Variante auf diesem Setup.

### Sauberere spaetere Variante

- eigener Batch-Worker statt HTTP-Sync-API
- n8n schreibt Job in Queue oder startet Worker-Prozess
- Worker speichert Fortschritt pro Segment
- n8n pollt nur den Status

Das ist langfristig die bessere Form fuer:

- grosse Dateien
- mehrere Jobs
- Retry
- Monitoring

## Was ich fuer n8n explizit nicht empfehlen wuerde

- lange 1:1-HTTP-Requests gegen `POST /audio` fuer komplette Stunden-Audios
- Container-Restarts als regulaeren Retry-Mechanismus
- ein einziges riesiges Audio ohne Segmentierung
- Mischbetrieb ohne persistente Zwischenartefakte

## Was ich fuer n8n explizit empfehlen wuerde

- persistente Arbeitsverzeichnisse
- segmentweise Outputs
- Resume-Mechanik
- Trennung zwischen kurzer Sync-Transkription und langem Batch-Job
- Conda-gebundene Toolausfuehrung fuer Download, Segmentierung und Python-Worker
- Modellwahl als konfigurierbaren Parameter

## Praktische Dateipfade aus diesem Lauf

Nutzbare Beispielpfade:

- Vollaudio: [/tmp/faster_whisper_youtube/full/full.m4a](/tmp/faster_whisper_youtube/full/full.m4a)
- 10-Minuten-Chunks: `/tmp/faster_whisper_youtube/chunks/`
- 5-Minuten-Chunks: `/tmp/faster_whisper_youtube/chunks5/`
- 2,5-Minuten-Chunks: `/tmp/faster_whisper_youtube/chunks2p5/`
- zusammengesetztes Transkript: [full_transcript.txt](/tmp/faster_whisper_youtube/final/full_transcript.txt)

## Empfohlener naechster Schritt

Der sinnvollste naechste Schritt ist nicht noch mehr Shell-Glue, sondern:

1. ein kleines persistentes Python-Worker-Skript ins Repo zu legen
2. dieses Skript von n8n per `Execute Command` aufzurufen
3. den HTTP-Container nur fuer kurze Einzeljobs zu behalten

Wenn das umgesetzt wird, ist der spaetere n8n-Flow deutlich einfacher, stabiler und nachvollziehbarer als der aktuelle Mischbetrieb aus HTTP-API und ad-hoc-Shell-Kommandos.
