# faster-whisper HTTP Service

Lokaler HTTP-Service fuer Audio-Transkription mit
[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper), FastAPI und
Docker Compose.

Der Service ist fuer zwei Betriebsarten gedacht:

- kurze Audiodateien synchron per `POST /audio`
- grosse Audiodateien als segmentierter Batch-Workflow mit Chunks, Resume und
  getrennten Zwischenergebnissen

## Repository-Inhalt

```text
app/main.py                 FastAPI-Service
docker-compose.yml          Docker-Setup und Runtime-Konfiguration
Dockerfile                  Container-Image
requirements.txt            Python-Abhaengigkeiten
FLOW_N8N_TRANSCRIPTION.md   Notizen und Zielbild fuer n8n/Batch-Workflows
```

Lokale Modell- und Cache-Dateien liegen standardmaessig unter `./config`.
Dieser Ordner ist bewusst per `.gitignore` ausgeschlossen und wird nicht nach
GitHub gepusht.

## Voraussetzungen

- Docker und Docker Compose
- Python 3.10+ oder 3.11+ fuer die Hugging Face CLI auf dem Host
- genug lokaler Speicherplatz fuer die Modelle

Ungefaehre Modellgroessen:

- `small`: einige hundert MB
- `large-v3`: mehrere GB

## Hugging Face CLI installieren

Aktuelle Hugging-Face-Installationen stellen die CLI als `hf` bereit:

```bash
python3 -m pip install -U huggingface_hub
hf --help
```

Optional einloggen:

```bash
hf auth login
```

Fuer die oeffentlichen Systran-Modelle ist normalerweise kein Token noetig. Ein
Login ist nur relevant, wenn ein privates oder zugriffsbeschraenktes Modell
verwendet wird. Tokens gehoeren nicht in `.env`, `docker-compose.yml`,
README-Dateien oder Git-History.

Aeltere Installationen kennen teilweise noch `huggingface-cli`. Fuer neue Setups
ist `hf download` die bevorzugte Form.

## Modelle herunterladen

Der Container mountet `./config` nach `/models`. Deshalb sollten die Modelle auf
dem Host in `./config` gecached werden:

```bash
mkdir -p config
hf download Systran/faster-whisper-small --cache-dir ./config
hf download Systran/faster-whisper-large-v3 --cache-dir ./config
```

Vor einem grossen Download kann ein Dry-Run sinnvoll sein:

```bash
hf download Systran/faster-whisper-large-v3 --cache-dir ./config --dry-run
```

Der Service laeuft standardmaessig im Offline-Modus:

```yaml
WHISPER_LOCAL_FILES_ONLY=true
WHISPER_CACHE_DIR=/models
```

Wenn `WHISPER_PRELOAD=true` gesetzt ist und das konfigurierte Modell lokal
fehlt, schlaegt der Containerstart fehl. In dem Fall erst das Modell
herunterladen oder temporaer `WHISPER_PRELOAD=false` setzen.

## Start mit Docker Compose

Image bauen und Service starten:

```bash
docker compose up -d --build
```

Logs ansehen:

```bash
docker compose logs -f faster-whisper
```

Service stoppen:

```bash
docker compose down
```

Healthcheck:

```bash
curl -sS http://localhost:10300/health
```

Beispielantwort:

```json
{
  "status": "ok",
  "default_model": "large-v3",
  "loaded_models": ["large-v3"],
  "cache_dir": "/models"
}
```

## Runtime-Konfiguration

Die Runtime wird in `docker-compose.yml` ueber Environment-Variablen gesteuert.

| Variable | Default im Compose | Bedeutung |
| --- | --- | --- |
| `WHISPER_MODEL` | `large-v3` | Standardmodell fuer `/audio` |
| `WHISPER_DEVICE` | `cpu` | Geraet, z.B. `cpu` oder `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | Compute-Typ, z.B. `int8`, `float16`, `float32` |
| `WHISPER_BEAM` | `5` | Beam Size fuer Transkription |
| `WHISPER_LANG` | `auto` | Sprache, z.B. `de`, `en` oder `auto` |
| `WHISPER_VAD` | `true` | Voice Activity Detection aktivieren |
| `WHISPER_CPU_THREADS` | `4` | CPU-Threads fuer das Modell |
| `WHISPER_PRELOAD` | `true` | Modell beim Start laden |
| `WHISPER_CACHE_DIR` | `/models` | Modellcache im Container |
| `WHISPER_LOCAL_FILES_ONLY` | `true` | Keine Hub-Downloads zur Laufzeit |
| `LOG_LEVEL` | `INFO` | Logging-Level |

Nach Aenderungen an der Compose-Konfiguration:

```bash
docker compose up -d --build
```

## Modell wechseln: small und large-v3

### Standardmodell dauerhaft wechseln

In `docker-compose.yml`:

```yaml
environment:
  - WHISPER_MODEL=small
```

oder:

```yaml
environment:
  - WHISPER_MODEL=large-v3
```

Danach den Service neu starten:

```bash
docker compose up -d
```

### Modell pro Request wechseln

Der Endpoint akzeptiert `model` als Formularfeld. Damit kann ein anderes lokal
verfuegbares Modell pro Request verwendet werden:

```bash
curl -sS -X POST http://localhost:10300/audio \
  -F "audio=@sample.wav" \
  -F "model=small" \
  -F "language=de" \
  -F "task=transcribe"
```

`small` ist deutlich schneller und eignet sich gut fuer lange Batch-Jobs oder
Tests. `large-v3` ist langsamer, liefert aber typischerweise bessere Qualitaet.

## HTTP-Endpunkte

### `GET /health`

Gibt den Status, das Standardmodell, geladene Modelle und den Modellcache zurueck.

```bash
curl -sS http://localhost:10300/health
```

### `POST /audio`

Transkribiert oder uebersetzt eine Audiodatei.

Pflichtfeld:

- `audio`: Audiodatei als Multipart-Upload

Optionale Formularfelder:

- `language`: z.B. `de`, `en`, `auto`
- `task`: `transcribe` oder `translate`
- `model`: z.B. `small` oder `large-v3`
- `beam_size`: z.B. `1`, `3`, `5`
- `vad_filter`: `true` oder `false`
- `initial_prompt`: optionaler Prompt fuer Kontext/Schreibweise

Beispiel:

```bash
curl -sS -X POST http://localhost:10300/audio \
  -F "audio=@sample.wav" \
  -F "language=de" \
  -F "task=transcribe" \
  -F "model=large-v3" \
  -F "beam_size=5" \
  -F "vad_filter=true"
```

Antwortschema:

```json
{
  "text": "Transkribierter Text",
  "language": "de",
  "language_probability": 0.99,
  "duration": 123.45,
  "duration_after_vad": 118.2,
  "model": "large-v3",
  "task": "transcribe",
  "segments": [
    {
      "id": 1,
      "start": 0.0,
      "end": 5.2,
      "text": "..."
    }
  ]
}
```

Ungueltige Audiodateien werden als `400` beantwortet. Unerwartete Fehler liefern
`500` mit einer Fehlermeldung im JSON-Body.

## Best Practices fuer grosse Audiodateien

Lange Audiodateien sollten nicht als ein einziger synchroner HTTP-Request
transkribiert werden. Auf CPU ist das langsam, schwer wiederaufzunehmen und
anfaellig fuer Timeouts oder Container-Restarts.

Empfohlene Strategie:

- Audio zuerst in Chunks schneiden
- Chunks einzeln transkribieren
- jedes Chunk-Ergebnis als eigene JSON-Datei speichern
- bereits fertige Chunks bei Wiederholung ueberspringen
- am Ende alle Segmente in Reihenfolge zusammensetzen

Empfohlene Chunk-Groessen:

- 120 bis 180 Sekunden fuer robuste CPU-Laeufe
- 300 Sekunden, wenn das Setup stabil ist
- groessere Chunks nur bei ausreichend RAM/GPU und guter Fehlerkontrolle

Beispiel mit `ffmpeg`:

```bash
mkdir -p chunks
ffmpeg -i input.m4a \
  -f segment \
  -segment_time 150 \
  -reset_timestamps 1 \
  -c copy \
  chunks/chunk_%04d.m4a
```

Wenn `-c copy` bei einem Format unpassende Schnitte erzeugt, Audio neu codieren:

```bash
ffmpeg -i input.m4a \
  -f segment \
  -segment_time 150 \
  -reset_timestamps 1 \
  -ac 1 \
  -ar 16000 \
  chunks/chunk_%04d.wav
```

Fuer sehr lange Dateien ist ein direkter Batch-Worker oft stabiler als viele
lange HTTP-Requests gegen den Container. Der HTTP-Service bleibt dann der
Fast-Path fuer kurze Audios, waehrend n8n oder ein lokales Skript Download,
Chunking, Retry, Resume und Zusammenbau steuert.

## Workflow-Hinweise

### Kurze Audios

1. Datei entgegennehmen
2. `POST /audio` aufrufen
3. JSON-Antwort speichern oder weiterverarbeiten

Das passt fuer kurze Clips, Ad-hoc-Uploads und interaktive Nutzung.

### Lange Audios, YouTube, Podcasts, Calls

1. Quelle herunterladen, z.B. mit `yt-dlp`
2. Audio mit `ffmpeg` in Chunks schneiden
3. Chunks einzeln transkribieren
4. Ergebnisse pro Chunk speichern
5. fehlgeschlagene Chunks gezielt wiederholen
6. finalen Text und optional eine Segment-JSON erzeugen

Weitere Details, Stolperfallen und n8n-Zielarchitektur stehen in
[`FLOW_N8N_TRANSCRIPTION.md`](FLOW_N8N_TRANSCRIPTION.md).

## Sicherheit und Git-Hygiene

- keine Tokens, PATs, API-Keys oder Secrets committen
- keine `.env`-Dateien committen
- `config/` nicht committen, da dort lokale Modelle und Cache-Daten liegen
- grosse Audio-/Video-Dateien nicht committen
- Secrets nur ueber lokale Shell, Secret-Store, CI-Secrets oder Docker-Secret-
  Mechanismen bereitstellen

Vor einem Push kann der geplante Inhalt kontrolliert werden:

```bash
git status --short --ignored
git ls-files
git grep -n -I -i -E 'pat_|github_pat|ghp_|api[_-]?key|secret|token|password|bearer|authorization'
```

## Weiterfuehrende Links

- Hugging Face CLI:
  <https://huggingface.co/docs/huggingface_hub/guides/cli>
- Hugging Face CLI Reference:
  <https://huggingface.co/docs/huggingface_hub/package_reference/cli>
- faster-whisper:
  <https://github.com/SYSTRAN/faster-whisper>
