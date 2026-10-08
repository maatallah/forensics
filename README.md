# Storage Forensics Collector (SFC)

Collecteur Enterprise de métadonnées de stockage Windows (disques locaux, SAN, NAS, SMB, DFS) destiné à alimenter la plateforme d'analyse **SFA**.
Parcours **unique**, **streaming**, **parallèle**, mémoire **bornée**. Python ≥ 3.12, **aucune dépendance d'exécution** ; XlsxWriter est une option purement locale (tableau de bord Excel).

## Installation

```powershell
git clone https://github.com/maatallah/forensics
cd forensics
python -m pip install -e .[dev]
```

Pour le tableau de bord Excel (`--excel`, `dashboard`), ajoutez l'extrait `excel` :

```powershell
python -m pip install -e ".[excel]"
```

## Utilisation

```powershell
sfcollect scan --targets D:\ R:\ \\server\finance --workers 8 --top-files 1000 --min-duplicate-size-mb 100 --output reports
# avec tableau de bord Excel en sortie :
sfcollect scan --targets D:\ --output reports --excel
# ou sans installation :
python -m cli.main scan --targets D:\ --output reports
```

Tableau de bord à partir d'exports déjà produits (aucun rescanner) :

```powershell
sfcollect dashboard reports\v2\H_20261007-1529
sfcollect dashboard reports\v2                     # dossier contenant un seul scan
sfcollect dashboard reports\v2\H_20261007-1529 --output reports\v2\synthese.xlsx --max-rows 5000
```

> **Note :** la commande s'appelle `sfcollect` car `sfc` est une commande Windows réservée (System File Checker, `System32\sfc.exe`). `python -m cli.main` reste utilisable.

> PowerShell/cmd : n'écrivez pas `"D:\"` entre guillemets (le `\"` final échappe le guillemet). Utilisez `D:\` ou `"D:\\"`.

| Option | Défaut | Rôle |
|---|---|---|
| `--targets` | requis | Racines à scanner (lecteurs, UNC, DFS) |
| `--workers` | 8 | Threads de scan simultanés |
| `--top-files` | 1000 | Taille du Top-N des plus gros fichiers |
| `--min-duplicate-size-mb` | 100 | Seuil de taille des doublons candidats |
| `--output` | `reports` | Dossier de sortie |
| `--split-depth` | 1 | Profondeur de découpage en unités de travail |
| `--memory-limit-mb` | 500 | Seuil RSS déclenchant les purges (0 = off) |
| `--max-directories` | 300000 | Nb max de dossiers suivis avant roll-up |
| `--progress-interval` | 5 | Secondes entre deux lignes de progression |
| `--quiet` / `--log-file` | | Silence / fichier de log (défaut `<output>/sfc.log`) |
| `--excel` | off | Génère `<Préfixe>_Dashboard.xlsx` après les exports (nécessite XlsxWriter) |

La sous-commande `dashboard` travaille **uniquement** à partir des exports existants :

| Argument | Défaut | Rôle |
|---|---|---|
| `SOURCE` | requis | Préfixe d'exports, fichier d'export ou dossier contenant un seul scan |
| `--output` | `<préfixe>_Dashboard.xlsx` | Fichier `.xlsx` de destination |
| `--max-rows` | 100000 | Plafond de lignes des feuilles volumineuses (répertoires, doublons) |

Code retour : `0` OK, `1` cible inaccessible (ou échec du tableau de bord pour `dashboard`), `2` argument invalide, `130` interrompu (Ctrl+C, résultats partiels écrits).
Un `--excel` qui échoue (XlsxWriter absent, fichier ouvert dans Excel) n'affiche qu'un avertissement : **le code retour du scan est inchangé**.

## Architecture

```text
SFC/
├── collector/
│   ├── enumerator.py          os.scandir itératif, erreurs classifiées, chemins longs \\?\
│   ├── scanner.py             plan -> workers -> reducer, progression, surveillance mémoire
│   ├── aggregators.py         TopFiles (min-heap), DirectoryTotals, ExtensionTotals, Aggregates
│   ├── duplicate_detector.py  candidats par taille, bornés
│   ├── age_analysis.py        5 buckets d'âge
│   ├── serialization.py       exports TSV UTF-8 + Summary.txt
│   ├── excel_dashboard.py     tableau de bord .xlsx à partir des TSV (optionnel)
│   └── models.py              config, limites, PartialResult, snapshots
├── cli/main.py                commandes `sfcollect scan` et `sfcollect dashboard`
├── reports/  tests/  pyproject.toml  README.md  CHANGELOG.md
```

```text
Targets ─► plan (WorkUnit = dossier racine) ─► round-robin entre cibles
              │
   Worker ─► PartialResult   (état privé, jamais partagé)
   Worker ─► PartialResult
   Worker ─► PartialResult
              ▼
   Reducer (thread principal) ─► Final Result ─► TSV / Summary
```

## Choix techniques

* **`os.scandir` + pile explicite** : sous Windows, taille et mtime proviennent de l'énumération du répertoire : `entry.stat()` ne coûte **aucun appel système supplémentaire**. Pas de récursion, pas de `pathlib.glob`, pas de liste de fichiers. Chaque fichier est un tuple éphémère libéré immédiatement ; la chaîne du dossier est partagée par tous ses fichiers.
* **Liens symboliques et jonctions non suivis** : pas de boucles, pas de double comptage (DFS/SMB).
* **Threads** : `scandir` relâche le GIL pendant les appels système ; le scan est limité par la latence E/S, pas par le CPU. Une cible unique est découpée en unités (dossiers de premier niveau, `--split-depth`) pour paralléliser aussi un seul volume.
* **Worker → Partial Result → Reducer** : chaque worker possède son `PartialResult` ; il est transmis via `Future` puis fusionné uniquement par le thread principal. **Aucun dictionnaire partagé, aucun verrou nécessaire.** Les compteurs de progression sont des entiers écrits par un seul worker et lus par le thread principal.
* **Top-N** : min-heap `heapq` ; rejet O(1) (`threshold`), insertion O(log N). Le chemin n'est même pas construit pour les fichiers sous le seuil.
* **Âge** : `bisect` sur 4 bornes, 5 compteurs constants, référence de temps unique par scan (basée sur `mtime`).
* **Doublons (V2)** : regroupement par taille exacte (pas de lecture de contenu) pour `size ≥ seuil`. Ce sont des **candidats**, non des doublons confirmés.
* **Exports** : écriture en flux ; UTF-8, séparateur TAB ; tabulations/retours à la ligne des chemins remplacés par des espaces ; surrogates Windows remplacés.

### Gestion mémoire (objectif < 500 MB pour 10 M+ fichiers)

| Structure | Borne | Mécanisme de purge |
|---|---|---|
| Inventaire fichiers | **jamais stocké** | streaming |
| Top-N | N éléments | min-heap |
| `directory_totals` | `--max-directories` (global ; réparti par worker, plancher 20 000) | **roll-up** des plus petits dossiers dans leur parent : totaux exacts, granularité réduite |
| `extension_totals` | 20 000 extensions | débordement vers `<other>` |
| Doublons | 300 000 tailles distinctes, 25 chemins/groupe (le compteur reste exact) | purge des plus petites tailles *singleton* ; compteurs `purged`/`dropped` dans le résumé |
| Âge / totaux | constants | — |
| Pression mémoire | `--memory-limit-mb` | RSS vérifié ~1 s ; au-dessus du seuil, workers et reducer compactent dossiers et singletons ; levée à 85 % |

Budget indicatif (~300 B / dossier) : reducer 300 k dossiers ≈ 90 MB, doublons ≈ 90 MB, 8 workers ≈ 100 MB, interpréteur ≈ 30 MB. Toutes les limites sont **souples** : elles déclenchent une purge, jamais un arrêt. Limites : un roll-up fait remonter les petits dossiers à leur parent dans `Directories.tsv` ; une purge de doublons peut sous-estimer un groupe.

### Robustesse

`Access Denied`, `Path Too Long` (préfixe `\\?\` automatique au-delà de 248 caractères, puis erreur 206 comptée), fichiers supprimés pendant le scan, erreurs inattendues d'un worker : **journalisés** (`sfc.log`), comptés dans `Summary.txt`, le scan continue. Une cible introuvable n'arrête pas les autres. Ctrl+C arrête proprement et écrit les résultats partiels.

### Progression

Une ligne par cible toutes les `--progress-interval` s (stderr) : Target, Root folders (unités terminées/total), Files Scanned, Data Volume, Elapsed, Files/s, MB/s, ETA. L'ETA est une heuristique : volume utilisé du disque/partage racine (`shutil.disk_usage`) ou, sinon, proportion d'unités terminées.

## Exports (`--output`)

`<Cible>_<AAAAMMJJ-HHMM>_` = nom dérivé de la cible + date/heure de début de scan, ex. `D_20261007-1050_Files.tsv` ou `server_finance_20261007-1050_Summary.txt`. Aide : `sfcollect --help`, `sfcollect scan --help`.

| Fichier | Colonnes |
|---|---|
| `<Préfixe>Files.tsv` | Rank, SizeBytes, HumanSize, LastModified, Extension, Path |
| `<Cible>_Directories.tsv` | Path, Files, Bytes, HumanSize (taille directe) |
| `<Cible>_Extensions.tsv` | Extension, Files, Bytes, HumanSize, PercentBytes |
| `<Cible>_AgeBuckets.tsv` | Bucket, Files, Bytes, HumanSize |
| `<Cible>_Duplicates.tsv` | SizeBytes, SizeHuman, Count, Path |
| `<Cible>_Summary.txt` | voir exemple |
| `<Préfixe>_Dashboard.xlsx` | 8 feuilles : tableau de bord, plus gros fichiers, répertoires, extensions, âge, doublons, résumé, données graphiques (optionnel) |

### Tableau de bord Excel (`--excel` / `sfcollect dashboard`)

Le classeur est reconstruit **à partir des exports TSV** : aucun rescanner, aucun appel système.
Il faut XlsxWriter (`pip install -e ".[excel]"`) ; sans lui, la commande affiche un message clair
et le reste du scan fonctionne normalement.

* **Bannière** : cible, horaires, durée, statut (dont `INTERROMPU` si le scan l'était).
* **6 tuiles KPI** : fichiers scannés, volume total, plus gros fichier, potentiel doublons
  récupérable, part des données les plus anciennes, erreurs de lecture (accès refusés, chemins
  trop longs, …). Les totaux sont recalculés depuis `Extensions.tsv`, exacts même après les
  purges mémoire du scan.
* **4 graphiques** : répartition du volume par extension (anneau), âge des données (colonnes +
  courbe des nombres de fichiers), top des répertoires, top des extensions par nombre de fichiers.
* **Feuilles tabulaires** avec filtres natifs et barres de données ; répertoires et doublons
  plafonnés à `--max-rows` lignes (les TSV restent la source complète, une note le rappelle dans
  le classeur).
* Valeurs écrites **typées** (nombres, dates) pour que tri, filtres et graphiques fonctionnent ;
  les chemins ne sont jamais interprétés comme formules ni URLs.

### Exemple de sortie

`D_AgeBuckets.tsv`
```text
Bucket	Files	Bytes	HumanSize
<30 jours	1204331	980345123456	913.04 GB
30-90 jours	2210443	1203345123456	1.09 TB
90-365 jours	3900112	4103345123456	3.73 TB
1-3 ans	2101233	3003345123456	2.73 TB
>3 ans	1203880	2903345123456	2.64 TB
```

`D_Duplicates.tsv`
```text
SizeBytes	SizeHuman	Count	Path
4294967296	4.00 GB	3	D:\Backups\vm1.vhdx
4294967296	4.00 GB	3	D:\Archive\vm1-copy.vhdx
4294967296	4.00 GB	3	D:\Old\vm1.bak.vhdx
```

`D_Summary.txt` (extrait)
```text
Storage Forensics Collector - Résumé du scan
============================================
Cible :                 D:\
Scan Start :            2026-10-07T10:50:00+01:00
Scan End :              2026-10-07T11:21:12+01:00
Duration :              00:31:12 (1872.0 s)
Files Scanned :         10,620,000
Total Size :            12.50 TB (13,743,895,347,200 octets)
Débit :                 5,673 fichiers/s, 7,341.8 Mo/s

Largest File :          412.00 GB (442,381,631,488 octets)  D:\SQL\big.mdf
Largest Directory :     1.80 TB (1,204 fichiers)  D:\Backups
...
```

Progression :
```text
[SCAN] Cible D:\ | Dossiers racines 3/12 | Fichiers scannés 1,234,567 | Volume données 45.20 GB | Écoulé 00:01:12 | 17,143 fichiers/s | 620.1 Mo/s | ETA 00:09:00 | RSS 310 Mo
```

## Tests et qualité

```powershell
python -m pytest
python -m mypy collector cli
python -m ruff check .
```

## Plan d'évolutions

1. **V3 doublons** : hash partiel (premiers/derniers 64 Ko) puis hash complet BLAKE3 uniquement pour les candidats ; gestion des hardlinks (file ID NTFS).
2. **Lecture MFT / USN Journal** pour les volumes NTFS locaux (×10–50 en vitesse) ; scans incrémentaux via USN.
3. **Multiprocessing** (un process par unité) pour dépasser le GIL sur énumération locale ultra-rapide, avec fusion via fichiers de résultats partiels.
4. **Spill sur disque** (SQLite/Parquet) pour `directory_totals` sans perte de granularité, et rollup inclusif (taille cumulée par sous-arbre).
5. **Métadonnées enrichies** : propriétaire, ACL, attributs (offline/cloud, compressé, chiffré), temps d'accès et de création.
6. **Export** Parquet/JSONL, push direct vers SFA (API), signature/horodatage des rapports pour la chaîne de custody.
7. **Découpage adaptatif** du travail (work-stealing quand un dossier racine domine) ; limitation de débit par partage SMB.
8. **Packaging** : exécutable autonome (PyInstaller/Nuitka), service Windows, planification.
