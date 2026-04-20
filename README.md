# Terminal Music Grid

This project has been created by traomeli (Raomelinary Tsiresy).

## Description

**Terminal Music Grid** est un lecteur audio en terminal avec interface TUI (Urwid), recherche de pistes en ligne, lecture via VLC (`cvlc`) et visualiseur audio en barres arc-en-ciel.

Le script:
- recherche des morceaux sur **SoundCloud** (si `SOUNDCLOUD_CLIENT_ID` est défini), sinon bascule vers **YouTube**, puis **Archive.org**
- lit les flux audio avec `cvlc`
- permet pause/reprise, piste suivante, pagination des résultats
- propose un mode fond (`q`) avec mini-visualiseur en terminal
- supporte le controle en arriere-plan via une commande shell `tmg` (FIFO de controle)

Fichier principal: `mp3.py`

## Auteurs

- **traomeli** — login 42
- **Raomelinary Tsiresy** — nom complet

## Prerequis

- Linux ou macOS
- Python 3.8+
- `pip`
- VLC avec binaire `cvlc`
- Acces internet

Dependances Python:
- `urwid`
- `yt-dlp`

Optionnel:
- variable d'environnement `SOUNDCLOUD_CLIENT_ID` pour activer la source SoundCloud

## Installation

### 1. Cloner le projet

```bash
git clone <URL_DU_REPO>
cd <NOM_DU_REPO>
```

### 2. Installer VLC (`cvlc`)

Linux (Debian/Ubuntu):
```bash
sudo apt update
sudo apt install -y vlc
```

macOS (Homebrew):
```bash
brew install --cask vlc
```

Verifier:
```bash
cvlc --version
```

### 3. Creer un environnement virtuel Python (recommande)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Installer les dependances Python

```bash
pip install --upgrade pip
pip install urwid yt-dlp
```

## Lancement

```bash
python3 mp3.py
```

## Utilisation

Dans l'interface:
- `Enter` lance la recherche
- `Space` pause/reprise
- `n` page suivante
- `p` page precedente
- `q` ferme l'UI mais laisse la musique en fond (mini-visualiseur terminal)
- `Esc` stoppe la lecture et quitte

## Controle musique en background via `.zshrc`

Pour controler Terminal Music Grid apres passage en mode fond, ajoutez ceci dans votre `~/.zshrc`:

```bash
# Controle de Terminal Music Grid
tmg() {
  if [ -e /tmp/tmg_ctrl.fifo ]; then
    echo "$1" > /tmp/tmg_ctrl.fifo
  else
    echo "TMG n'est pas lance (FIFO introuvable)."
  fi
}
```

Puis rechargez la configuration shell:

```bash
source ~/.zshrc
```

Commandes disponibles:
- `tmg p` : met en pause ou relance la lecture.
- `tmg n` : passe a la musique suivante.
- `tmg q` : arrete tout proprement (musique + HUD en haut de l'ecran).

## Activer SoundCloud (optionnel)

Par defaut, le script essaie SoundCloud si un `SOUNDCLOUD_CLIENT_ID` est present.

```bash
export SOUNDCLOUD_CLIENT_ID="votre_client_id"
python3 mp3.py
```

Sans cette variable, le script bascule automatiquement vers YouTube puis Archive.org.

## Structure

```text
.
├── mp3.py
├── README.md
└── LICENSE
```

## Depannage

- Erreur `pip install urwid` / `pip install yt-dlp`:
  les paquets ne sont pas installes dans l'environnement actif.
- Erreur `cvlc introuvable`:
  installez VLC et verifiez que `cvlc` est dans le PATH.
- Aucune piste trouvee:
  verifier la connexion internet ou changer le terme de recherche.

## Notes

- La lecture depend de services externes (YouTube, Archive.org, SoundCloud) et de leur disponibilite.
- Certaines pistes peuvent ne pas exposer de flux audio lisible.

## License

Ce projet est distribue sous licence MIT.
Voir le fichier [LICENSE](LICENSE).
