# GrooveBox

Web app per la registrazione e riproduzione di dischi in vinile, progettata per girare su **Raspberry Pi 4** con PipeWire e un preamplificatore phono USB (Terratec iVinyl).

## Funzionalità

- Registrazione audio 24-bit / 96kHz via PipeWire
- Monitoraggio in tempo reale dell'ingresso phono (jack o Bluetooth)
- Riproduzione dei file registrati con avanzamento/rewind rapido
- Selezione uscita: **jack** (ALSA) o **Bluetooth**
- Spegnimento sicuro del Pi dalla UI
- Interfaccia web PWA (Progressive Web App), installabile su smartphone

## Requisiti di sistema

- Raspberry Pi 4 con Raspberry Pi OS (Bookworm o superiore)
- **PipeWire** con `pw-record`, `pw-loopback`, `pw-play`
- **ALSA** con `aplay`
- **SoX** (`sox`) per seek durante la riproduzione
- Python 3.9+
- Preamplificatore phono USB (es. Terratec PhonoPreAmp iVinyl)

## Installazione

```bash
# 1. Clona il repository
git clone https://github.com/luckehall/groovebox.git
cd groovebox

# 2. Crea e attiva un ambiente virtuale
python3 -m venv .venv
source .venv/bin/activate

# 3. Installa le dipendenze Python
pip install -r requirements.txt

# 4. Configura l'ambiente
cp .env.example .env
nano .env  # adatta i nomi dei dispositivi audio al tuo hardware
```

## Configurazione

Tutte le variabili di configurazione si trovano in `.env` (vedi [.env.example](.env.example)):

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `RECORDINGS_DIR` | `/mnt/groovebox/Registrazioni` | Directory di salvataggio |
| `PLAYBACK_JACK` | `plughw:CARD=Headphones,DEV=0` | Dispositivo ALSA uscita jack |
| `SAMPLE_RATE` | `96000` | Frequenza di campionamento (Hz) |
| `CHANNELS` | `2` | Canali audio (stereo) |
| `FF_RW_SECONDS` | `10` | Secondi di avanzamento/rewind |
| `PW_SOURCE` | `alsa_input.usb-Terratec_...` | Nodo PipeWire sorgente |
| `PW_SINK` | `alsa_output.platform-...` | Nodo PipeWire sink jack |
| `PW_SINK_BT` | `bluez_output.XX_XX_...` | Nodo PipeWire sink Bluetooth |
| `PORT` | `5001` | Porta HTTP del server |

Per trovare i nomi dei nodi PipeWire sul tuo sistema:
```bash
pw-cli list-objects | grep node.name
```

## Avvio

```bash
source .venv/bin/activate
python groovebox-server.py
```

Apri il browser su `http://<ip-del-pi>:5001`.

### Avvio automatico con systemd

```ini
# /etc/systemd/system/groovebox.service
[Unit]
Description=GrooveBox Server
After=pipewire.service sound.target

[Service]
User=pi
WorkingDirectory=/home/pi/groovebox
EnvironmentFile=/home/pi/groovebox/.env
ExecStart=/home/pi/groovebox/.venv/bin/python groovebox-server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now groovebox
```

## API

| Metodo | Endpoint | Descrizione |
|--------|----------|-------------|
| GET | `/api/status` | Stato corrente (status, filename, seconds, levels) |
| GET | `/api/files` | Lista file registrati |
| GET | `/api/files/<filename>` | Scarica un file WAV |
| POST | `/api/rec` | Avvia / riprendi registrazione |
| POST | `/api/stop` | Ferma registrazione o riproduzione |
| POST | `/api/pause` | Pausa / riprendi |
| POST | `/api/play` | Avvia riproduzione `{"filename": "Vinile_*.wav"}` |
| POST | `/api/ff` | Avanzamento rapido (+10s) |
| POST | `/api/rw` | Rewind (-10s) |
| POST | `/api/output` | Seleziona uscita `{"mode": "jack"\|"bt"}` |
| POST | `/api/shutdown` | Spegni il Raspberry Pi |

## Struttura del progetto

```
groovebox/
├── groovebox-server.py   # Backend Flask + logica audio
├── groovebox.html        # Frontend (PWA single-page)
├── manifest.json         # Web App Manifest
├── icon-192.png          # Icona PWA
├── icon-512.png          # Icona PWA
├── requirements.txt      # Dipendenze Python
├── .env.example          # Template configurazione
└── .gitignore
```

## Licenza

[MIT](LICENSE)
