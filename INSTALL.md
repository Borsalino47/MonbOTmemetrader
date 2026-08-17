# CRYPTO PULSE AI — Guide d'installation

Ce guide est écrit pour quelqu'un qui n'est pas développeur. Aucune connaissance
en programmation n'est nécessaire. Vous allez taper des commandes ; vous n'avez
pas besoin de comprendre ce qu'elles contiennent.

**Trois choses à savoir avant de commencer :**

1. Ce logiciel ne passe **aucun ordre**. Il ne peut pas acheter, vendre, retirer
   ni transférer quoi que ce soit. Il lit des prix publics et les classe.
   Aucun code de trading automatique n'existe dans ce projet.
2. Il n'a besoin d'**aucune clé d'API**. Les données de marché publiques
   n'exigent pas de compte. Ne mettez jamais de clé nulle part ici.
3. Le score affiché est un **classement sur 100, pas une probabilité**. Un score
   de 84 veut dire « ce setup se classe au-dessus d'un setup à 60 selon les
   pondérations actuelles ». Cela ne veut pas dire « 84 % de chances ».

---

## Sur ordinateur (Windows, Mac, Linux)

### Étape 1 — Installer Python

Allez sur **https://www.python.org/downloads/** et installez Python 3.11 ou plus
récent.

> **Windows uniquement** : sur le premier écran de l'installateur, cochez la case
> **« Add Python to PATH »** en bas. C'est la seule case qui compte. Si vous
> l'oubliez, les commandes suivantes ne fonctionneront pas.

### Étape 2 — Ouvrir un terminal

* **Windows** : touche Windows, tapez `powershell`, ouvrez « Windows PowerShell ».
* **Mac** : touche Cmd + Espace, tapez `terminal`, ouvrez « Terminal ».
* **Linux** : vous savez déjà.

### Étape 3 — Trois commandes

Copiez-collez ces lignes **une par une**, en appuyant sur Entrée après chacune et
en attendant qu'elle se termine.

```bash
git clone https://github.com/Borsalino47/MonbOTmemetrader.git
cd MonbOTmemetrader
./start.sh
```

> **Windows** : remplacez la troisième ligne par `bash start.sh`. Si `bash`
> n'existe pas, utilisez plutôt :
> ```
> python -m venv .venv
> .venv\Scripts\pip install -e .
> .venv\Scripts\python -m cryptopulse.cli doctor
> ```

C'est tout. `start.sh` installe ce qui manque, vérifie que le flux de données
répond, puis lance un scan.

---

## Sur Android (téléphone ou tablette)

### L'architecture, en une image

Sur Android, CRYPTO PULSE tourne dans **deux environnements à la fois**, et
c'est volontaire :

```
   ┌─────────────────────────────────────────────────────────┐
   │  TERMUX NATIF                                           │
   │  Node.js / npm / Vite   →  construit  frontend/dist     │
   │                                                         │
   │      ↓ le dossier du projet est monté au MÊME chemin    │
   │                                                         │
   │  UBUNTU 24.04 (proot-distro)                            │
   │  Python 3.12 / FastAPI / SQLite  →  fait tourner        │
   │  le serveur, la base, les scans                         │
   └─────────────────────────────────────────────────────────┘
```

**Build du frontend = Termux natif. Backend = Ubuntu sous PRoot.** Aucune des
deux moitiés ne peut faire le travail de l'autre :

* `npx vite build` **plante en BUS ERROR** sous Ubuntu/PRoot. esbuild émet des
  instructions que la traduction d'appels système de PRoot ne supporte pas. Ce
  n'est pas un problème de réglage : le build doit tourner dans Termux.
* `numpy` et `pydantic-core` ne se compilent pas proprement dans Termux natif.
  Le Python qui marche est celui d'Ubuntu.

**Vous n'avez jamais à savoir de quel côté va une commande.** Les trois scripts
s'en chargent seuls ; vous ne tapez jamais `proot-distro login` vous-même. Tout
se lance **depuis Termux**.

> **Un seul dépôt, pas deux.** Le projet vit une fois, dans le home Termux, et
> est monté dans Ubuntu **au même chemin**. `frontend/dist`, `.env` et
> `data/cryptopulse.db` sont donc littéralement les mêmes fichiers des deux
> côtés : rien à copier, et aucun moyen que deux copies divergent sans que vous
> puissiez dire laquelle le serveur a lue.

### Étape 1 — Installer Termux

Installez **Termux** depuis **F-Droid** : https://f-droid.org/packages/com.termux/

> N'installez pas Termux depuis le Play Store. La version du Play Store n'est
> plus mise à jour et les paquets Python y sont cassés. C'est la cause d'échec
> la plus fréquente sur Android.

### Étape 2 — Récupérer le projet

Ouvrez Termux et tapez :

```bash
pkg install -y git
git clone https://github.com/Borsalino47/MonbOTmemetrader.git
cd MonbOTmemetrader
```

### Étape 3 — Les trois scripts

Trois commandes, à trois moments différents. **Toutes depuis Termux.**

```bash
./android-install.sh    # UNE FOIS. Long : installe Ubuntu, compile numpy, construit l'interface.
./android-start.sh      # TOUS LES JOURS. Démarre, et rien d'autre.
./android-update.sh     # APRÈS UN git pull seulement.
```

Ce que chacun fait, et de quel côté :

| | Termux natif | Ubuntu (PRoot) |
|---|---|---|
| **`android-install.sh`** | installe `proot-distro`, `nodejs-lts`, puis `npm install` + `vite build` | installe Ubuntu, `python3-venv`, `.venv`, les dépendances, les icônes, puis un `doctor` bloquant |
| **`android-update.sh`** | `npm install` + `vite build`, **seulement si une source a changé** | sauvegarde SQLite, `pip install -e .`, migration additive du schéma, vérification finale |
| **`android-start.sh`** | rien du tout | lance uvicorn sur `127.0.0.1:8000` |

`android-install.sh` écrit un fichier repère dans Ubuntu
(`/etc/cryptopulse-inside-distro`). C'est lui, et rien d'autre, qui permet aux
scripts de savoir de quel côté ils tournent — un fait qu'on crée, plutôt qu'une
coïncidence qu'on interprète : deviner à partir de `/etc/os-release` prend un
ordinateur de bureau sous Ubuntu pour un téléphone sous PRoot.

> **Si un script est lancé du mauvais côté, il le dit et s'arrête**, avec la
> phrase à taper pour se remettre dans le bon (`exit` pour sortir d'Ubuntu). Il
> ne tente jamais l'étape « quand même » : sous PRoot, ce serait un BUS ERROR
> plusieurs minutes plus tard, sans rapport visible avec la cause.

**Pourquoi trois scripts.** L'ancienne version faisait tout à chaque lancement :
vérifier pip, chercher si l'interface devait être reconstruite, puis attendre la
fin d'une vérification réseau **avant même de démarrer le serveur**. Mesuré :
3,6 s rien que pour la vérification, et 4,9 s de plus les jours de
reconstruction — sur un ordinateur de bureau. Sur un téléphone, bien davantage.
Et pendant tout ce temps, l'écran restait vide en attendant des données déjà
présentes dans votre base.

`./android-start.sh` ne fait plus **rien** d'autre que démarrer : pas de pip,
pas de npm, pas d'icônes, pas de vérification bloquante.

### Ce que vous voyez au démarrage

L'interface s'affiche **immédiatement** avec votre dernier scan enregistré,
accompagné de son âge exact. Aucun écran vide, même si Binance est lent ou
injoignable.

En haut, un bandeau dit où en est la vérification du flux :

| | Ce que ça veut dire |
|---|---|
| 🟡 **VÉRIFICATION BINANCE EN COURS** | La vérification tourne. Les données affichées viennent du dernier scan enregistré. |
| 🟢 **BINANCE LIVE VERIFIED** | Un vrai aller-retour réseau a renvoyé des données cohérentes avec elles-mêmes. |
| 🔴 **FLUX LIVE NON VÉRIFIÉ** | Échec. Vos scans précédents restent visibles avec leur âge, et un bouton « Réessayer » apparaît. |

**Tant que le flux n'est pas vérifié, aucune nouvelle décision ACHETER ou VENDRE
n'est présentée comme un signal en direct** — elle n'est ni journalisée ni
envoyée en notification. Les données précédentes restent visibles ; c'est la
prétention d'être « en direct » qui est retenue, pas l'information.

Puis, dans **Chrome** sur le téléphone :

1. ouvrez **http://127.0.0.1:8000**
2. menu **⋮** → **« Installer l'application »**

Une icône **CRYPTO PULSE** apparaît sur votre écran d'accueil. En la touchant,
l'application s'ouvre en plein écran, sans barre d'adresse — comme une vraie
application Android.

> **Pourquoi 127.0.0.1 et pas l'adresse Wi-Fi du téléphone ?** L'installation
> exige un « contexte sécurisé ». La norme considère `127.0.0.1` comme sécurisé
> au même titre que HTTPS, ce qui permet d'installer sans certificat. Depuis un
> autre appareil du réseau (192.168.x.x), la page s'affiche mais ne peut pas
> s'installer — l'application vous le dira elle-même.

Pour essayer sans vrai flux de données : `./android-start.sh demo`.

L'application garde en mémoire son interface pour s'ouvrir instantanément, mais
**jamais les prix** : ceux-ci sont toujours redemandés au serveur. Un prix affiché
est soit à jour, soit signalé comme ancien — jamais un vieux chiffre présenté
comme neuf.

---

## Les cinq commandes que vous utiliserez

Toutes se lancent depuis le dossier `MonbOTmemetrader`.

| Commande | Ce qu'elle fait |
|---|---|
| `./start.sh` | Vérifie le flux, puis affiche le classement des 30 meilleurs actifs |
| `./start.sh serve` | Lance le tableau de bord sur http://localhost:8000 |
| `./start.sh verify` | Dit ce que le prix a **réellement fait** 15min / 1h / 4h / 24h après chaque signal passé |
| `./start.sh demo` | Fonctionne hors-ligne avec des données **inventées**, pour essayer l'interface |
| `./start.sh kraken` | Utilise Kraken au lieu de Binance (utile si Binance est bloqué chez vous) |

**`verify` est la commande importante.** C'est elle qui vous dit si l'outil a eu
raison. Lancez-la après quelques jours d'utilisation : avant, il n'y a rien à
mesurer, et le tableau vous le dira au lieu d'inventer un chiffre.

---

## Comment lire l'écran

### Le bandeau en haut

* **DATA : LIVE** (vert) — les prix viennent d'une vraie bourse.
* **DATA : DEMO** (rouge) + bandeau orange — **tout ce qui est affiché est
  inventé**. Aucun prix, aucun score, aucun verdict ne vient d'un marché. C'est
  le mode `demo`, utile pour découvrir l'interface, inutile pour décider quoi
  que ce soit.

Il n'y a pas d'état intermédiaire, et le logiciel ne bascule jamais tout seul de
l'un à l'autre.

### Le verdict

Chaque ligne porte un des quatre verdicts :

| | Ce que ça veut dire |
|---|---|
| 🟢 **FORTE OPPORTUNITÉ** | Bon score, filtres propres, mouvement encore jeune, setup déclenché. Les quatre à la fois, sinon ce n'est pas ce niveau. |
| 🟡 **À SURVEILLER** | Il se passe quelque chose, mais ce n'est pas encore déclenché ou ça ne passe pas la barre. C'est l'état normal de la plupart des lignes. |
| 🟠 **RISQUÉ** | Le signal est réel, l'entrée est mauvaise : le mouvement est déjà bien avancé, ou de grosses pénalités de risque ont été appliquées. |
| 🔴 **ÉVITER** | Un filtre bloquant a rejeté l'actif (liquidité dangereuse, sécurité insuffisante), ou les données ne sont pas assez fiables pour juger. |

Un verdict résume les filtres du logiciel. **Ce n'est ni un conseil ni une
probabilité.** Les pondérations qui produisent ces scores n'ont jamais été
validées sur de vrais résultats — c'est écrit sous chaque verdict, et c'est
volontaire.

### Les onglets

* **Accueil** — les trois meilleurs setups et l'état du flux, en cinq secondes.
* **Scanner** — le classement en direct.
* **Recherche** — tout le venue classé par changement de comportement.
* **Choix** — vos propres décisions : ce que vous avez validé, surveillé, rejeté.
* **Alertes** — ce qui a franchi un seuil.
* **Vérification** — ce que le prix a fait après. **L'onglet qui compte.**
* **Performance** — les statistiques de réussite des signaux déjà tranchés.

### Les trois scores

Trois questions différentes, jamais mélangées en un seul chiffre :

| Score | La question | L'horizon |
|---|---|---|
| **Opportunité** | Est-ce un bon setup ? | heures à jours |
| **Recherche** | Le comportement de ce token vient-il de changer ? | heures |
| **Explosion 15 min** | Ça va bouger dans le quart d'heure ? | 15 minutes |

Ils sont souvent en désaccord, et c'est voulu. Un beau retest sur le graphique
journalier a un bon score d'opportunité et un score d'explosion proche de zéro :
il va se résoudre en plusieurs jours. Faire la moyenne des deux donnerait un
chiffre qui ne décrit ni l'un ni l'autre.

**Le score d'explosion est le seul dont la promesse est déjà mesurée.** Il dit
« dans 15 minutes », et l'onglet Vérification enregistre depuis toujours ce que
le prix a réellement fait 15 minutes après chaque signal. C'est donc le premier
chiffre de cette application qui pourra être prouvé faux — et c'est une qualité,
pas un défaut.

### Vos décisions

Sur la fiche de chaque token, quatre boutons : **✅ Valider**, **⭐ Surveiller**,
**🔬 Analyser**, **❌ Rejeter**.

Chaque décision est enregistrée avec ce qui était affiché à ce moment-là : le
prix, les trois scores, le verdict, les raisons et l'invalidation. Pas un lien
vers le scan — une photographie de l'écran. Un mois plus tard, vous pourrez
relire la décision dans les mots qui l'ont produite, même si les pondérations
ont changé entre-temps.

Changer d'avis **ajoute** une décision au lieu d'effacer la précédente. C'est la
suite des décisions qui est intéressante, pas la dernière.

L'onglet **Choix** ne montre aucun taux de réussite, et n'en montrera pas avant
longtemps. Un pourcentage sur quelques décisions se lirait comme un jugement sur
votre propre flair, et ce serait le chiffre le plus trompeur que ce logiciel
puisse afficher.


---

## Les six décisions

L'application ne se contente plus d'afficher des scores : elle dit quoi faire.

| | Quand | Ce que ça veut dire |
|---|---|---|
| 🟢 **ACHETER** | token non détenu | Tous les critères d'entrée passent en même temps |
| 🟡 **SURVEILLER** | les deux | Il se passe quelque chose, mais pas encore assez |
| ⚫ **NE PAS ACHETER** | token non détenu | Les conditions ne sont pas réunies, ou un risque bloque |
| 🔵 **CONSERVER** | position ouverte | La raison de l'achat tient toujours |
| 🟠 **RÉDUIRE / PROTÉGER** | position ouverte | Plusieurs signes de retournement, le gain mérite protection |
| 🔴 **VENDRE** | position ouverte | Sortie claire, ou setup invalidé |

**Le vert est réservé à une nouvelle entrée, le bleu à une position déjà
ouverte.** Ce n'est pas cosmétique : d'un coup d'œil vous devez distinguer
« il y a quelque chose à faire » de « il n'y a rien à faire ». Chaque décision
affiche toujours **icône + texte + couleur** — jamais la couleur seule, qui
disparaît au soleil sur un téléphone.

### 🟢 ACHETER n'apparaît pas facilement

Un seul score élevé ne suffit jamais. Sept critères doivent passer **en même
temps** : opportunité, explosion 15 min, confiance, sécurité, maturité,
liquidité, et un setup réellement déclenché. Voir zéro ACHETER pendant des
heures est le fonctionnement normal, pas une panne.

Les seuils sont réglables dans `.env` (`CP_TRADE_BUY_MIN_OPPORTUNITY=75`, etc.).
Ils ne sont **pas** validés : ce sont des hypothèses de départ, et c'est
précisément pour cela qu'ils sont des réglages et non des constantes.

### Un veto passe avant tout

Liquidité dangereuse, alerte sécurité, setup déjà invalidé : ⚫ **NE PAS
ACHETER**, quels que soient les scores. Pas un score réduit — un score réduit
laisserait quand même le token dangereux passer devant un token sain dans une
liste triée.

---

## Mes positions

### Après un 🟢 ACHETER

L'application demande : **AVEZ-VOUS ACHETÉ ?** — OUI ou NON.

* **OUI** ouvre une position, surveillée toutes les 15 secondes. Vous pouvez
  indiquer votre prix réel et le montant investi ; c'est facultatif, et la fiche
  précise ensuite quel prix a servi au calcul du rendement.
* **NON** est enregistré aussi, **et le signal reste suivi**. C'est la seule
  façon de savoir plus tard si votre hésitation avait raison. Un tableau de bord
  qui ne garderait que ce que vous avez suivi vous flatterait systématiquement.

Ne pas répondre n'est **pas** un « non ». Les recommandations sans réponse sont
comptées à part.

### Pendant que vous détenez

Le **Position Watcher** ne regarde que vos positions ouvertes, toutes les 15
secondes au lieu de 60. Il calcule une **santé de position** : « les raisons qui
avaient conduit à dire ACHETER sont-elles toujours valides ? »

> **La santé n'est pas le gain.** Une position à +30 % dont le setup est cassé
> est en mauvaise santé — et c'est exactement le moment où le gain est sur le
> point d'être rendu. Les deux chiffres sont affichés côte à côte et ont le
> droit de se contredire.

Si trop de données manquent, la santé s'affiche « INCONNUE » et la décision
devient 🟡 SURVEILLER, jamais 🔴 VENDRE. Vous dire de vendre parce qu'une requête
a échoué serait la pire fausse alerte possible.

### L'invalidation prime sur tout

Le niveau d'invalidation est fixé **au moment de l'achat**, avant que quoi que ce
soit ne soit émotionnel. S'il est franchi, l'application affiche immédiatement
🔴 **VENDRE** et « SETUP INVALIDÉ », même si le momentum et les scores restent
bons. Tout le reste est un faisceau d'indices ; celui-là est le contrat.

### L'écran ne clignote pas

Une décision doit se répéter deux cycles avant de changer l'affichage, et un
délai de 5 minutes empêche les allers-retours. **Sauf** vers la sortie : passer
à RÉDUIRE ou VENDRE n'attend jamais. Sortir en retard parce qu'un minuteur
tournait n'est pas un compromis acceptable.

### Après un 🔴 VENDRE

**AVEZ-VOUS VENDU ?** Si oui, indiquez éventuellement votre prix réel et la
position se clôture avec son rendement réalisé.

---

## Ce que l'application ne fera jamais

**Aucun ordre n'est passé automatiquement.** Il n'existe pas une ligne de code
capable de passer un ordre dans ce dépôt, aucune clé d'exchange, aucune
signature. Un test parcourt la table de routage de l'API à chaque exécution pour
vérifier qu'aucun point d'entrée ne pourrait le faire.

Le déroulé est toujours :

```
ANALYSE → DÉCISION → ALERTE → VOUS ACHETEZ VOUS-MÊME
        → VOUS CONFIRMEZ → SUIVI ET STATISTIQUES
```

---

## Mettre à jour sans perdre vos données

Sur Android, dans Termux, depuis le dossier `MonbOTmemetrader` :

```bash
cd ~/MonbOTmemetrader
git pull
./android-update.sh
./android-start.sh
```

`android-update.sh` sauvegarde votre base lui-même avant toute opération.

C'est tout. Détail de ce qui se passe, dans l'ordre :

1. **`git pull`** récupère le code. Vos fichiers `.env` et `data/` ne sont pas
   suivis par Git : ils ne peuvent pas être écrasés.
2. **Sauvegarde SQLite** — `data/cryptopulse.db.backup`. Le chemin de la base
   est **demandé à l'application** (`cryptopulse db-path`), pas lu dans le
   shell : `CP_DB_URL` vit dans `.env` et n'atteint jamais l'environnement du
   terminal. L'ancienne version la déréférençait quand même et s'arrêtait sur
   `CP_DB_URL: unbound variable` — l'étape de sauvegarde devenant la raison de
   son absence. La présence du fichier de sauvegarde est **vérifiée** avant de
   migrer, jamais supposée.
3. **Dépendances Python — dans Ubuntu.**
4. **Interface React/Vite — dans Termux natif**, et seulement si une source a
   changé depuis le dernier build.
5. **`frontend/dist` côté Ubuntu** — rien à copier : c'est le même dossier.
6. **Migration du schéma — dans Ubuntu.** Additive uniquement.
7. **`./android-start.sh`** démarre. C'est tout ce qu'il fait.

> **Vos données sont conservées.** La migration est *additive uniquement* : elle
> ajoute des colonnes et des tables, et **refuse** toute opération destructive
> plutôt que de la deviner. Vos signaux, vos vérifications, vos décisions et vos
> positions restent en place.

Si vous voyez un message `column_added` au démarrage, c'est le fonctionnement
normal : la nouvelle version a besoin de colonnes que votre base n'avait pas.

**Si quelque chose se passe mal** : `cp data/cryptopulse.db.backup
data/cryptopulse.db` restaure l'état d'avant.

Sur ordinateur, la même chose avec `./start.sh` à la place de
`./android-start.sh`.

---

## Si quelque chose ne marche pas

### « Je ne vois que le mode DEMO »

C'est que le flux de données n'a pas pu être joint. Lancez :

```bash
python -m cryptopulse.cli doctor
```

Cette commande fait un vrai appel réseau et vous dit **exactement** ce qui bloque,
avec la marche à suivre. Elle affiche `LIVE VERIFIED` quand tout va bien.

Causes fréquentes :

* **Binance bloqué dans votre pays** — essayez `./start.sh kraken`.
* **Pas de connexion internet** — le message le dira.
* **Pare-feu ou VPN d'entreprise** — le diagnostic distingue ce cas des autres.

### « L'onglet Verification est vide »

C'est normal au début. La première fenêtre se ferme 15 minutes après le premier
signal, la dernière un jour entier plus tard. Le tableau reste vide plutôt que
d'afficher un zéro : une fenêtre qui n'est pas terminée n'a pas de résultat, et
le logiciel ne l'invente pas.

### « python : commande introuvable » (Windows)

Python a été installé sans cocher « Add Python to PATH ». Réinstallez-le en
cochant la case.

### « BUS ERROR » pendant la construction de l'interface

Vous êtes dans Ubuntu, pas dans Termux. Tapez `exit` pour revenir à Termux, puis
relancez `./android-update.sh` depuis là. Les scripts refusent normalement ce
cas d'eux-mêmes ; si vous avez lancé `npx vite build` à la main, c'est la seule
manière de le voir.

### « Lancez ce script depuis TERMUX NATIF »

Même chose : `exit`, puis relancez. Le script s'arrête volontairement plutôt que
d'essayer — sous PRoot, l'échec arriverait plusieurs minutes plus tard et ne
ressemblerait plus à sa cause.

### « proot-distro est absent »

`./android-install.sh` n'a jamais été lancé, ou pas jusqu'au bout. Relancez-le :
il est ré-exécutable sans risque, et ne refait que ce qui manque.

### « npm: command not found »

Seul le tableau de bord graphique a besoin de `npm`. Le scan en ligne de commande
fonctionne sans. Installez Node.js depuis https://nodejs.org si vous voulez
l'interface.

---

## Recevoir les alertes sur votre téléphone

### Option 1 — Notifications Android (recommandé sur téléphone)

Si vous faites tourner l'application dans Termux sur votre téléphone, elle peut
vous notifier **directement**, sans compte, sans mot de passe et sans passer par
un service tiers. Il faut deux installations, et beaucoup de gens n'en font
qu'une :

1. l'**application Termux:API**, depuis F-Droid, comme Termux lui-même ;
2. le **paquet** : `pkg install termux-api`

Puis vérifiez que ça marche vraiment :

```bash
python -m cryptopulse.cli notify
```

Cette commande envoie une vraie notification. Si rien n'apparaît sur l'écran,
elle vous dit **laquelle des deux installations manque** — c'est la panne la
plus fréquente, et « les notifications ne marchent pas » sans plus de détail
n'aide personne.

> Seules les alertes **HIGH** et **CRITICAL** font sonner le téléphone. Une
> alerte WATCH qui vibre à 3h du matin, c'est ainsi qu'on finit par couper les
> notifications — et par rater la seule qui comptait. Un même token ne re-sonne
> que si son niveau **monte**.
>
> Pour tout recevoir, y compris les alertes calmes :
> `CP_ALERT_ANDROID_MIN_LEVEL=WATCH` dans le fichier `.env`.

### Option 2 — Webhook Discord ou Slack

Par défaut, les alertes n'existent que dans le tableau de bord — vous ne les
voyez donc que si vous le regardez. Pour les recevoir sur votre téléphone,
créez un webhook Discord ou Slack (dans Discord : *Paramètres du salon →
Intégrations → Créer un webhook → Copier l'URL*), puis ajoutez la ligne
suivante dans le fichier `.env` :

```
CP_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Redémarrez, c'est tout. Le format est détecté automatiquement.

> **Cette adresse est un mot de passe.** Toute personne qui la possède peut
> écrire dans votre salon. Ne la publiez nulle part. Le logiciel ne l'écrit
> jamais dans un journal ni dans une réponse d'API — seulement son domaine.
> Le fichier `.env` n'est jamais envoyé sur GitHub.

Si le webhook cesse de fonctionner, `http://localhost:8000/api/health` vous le
dira dans la section `alert_delivery` : un webhook mort ne doit pas ressembler
à un marché calme.

---

## Ce que ce logiciel ne fait pas

Dit clairement, pour qu'il n'y ait pas de malentendu :

* Il **ne trade pas**. Aucun ordre, aucun retrait, aucun transfert. Il n'y a pas
  une ligne de code capable de passer un ordre dans ce dépôt.
* Il **ne prédit pas**. Il classe des configurations selon des règles fixes et
  affiche pourquoi. Ces règles sont une hypothèse de départ, pas un modèle validé.
* Il **n'invente jamais une donnée**. Si un chiffre est inconnu, il affiche `—`
  et le signale, plutôt que d'écrire zéro.
* Il **ne bascule jamais en silence** sur des données inventées. Le mode DEMO est
  affiché en permanence, en rouge et en orange, sur chaque écran.

---

## Sécurité

* Aucune clé d'API n'est nécessaire, et aucune ne doit être ajoutée.
* Le fichier `.env` (vos réglages) n'est jamais envoyé sur GitHub.
* Le mode papier (`CP_PAPER_MODE=true`) est actif par défaut et doit le rester.

Si vous avez déjà mis une clé Binance quelque part dans ce projet par le passé,
**révoquez-la** sur votre compte Binance et créez-en une nouvelle. Une clé qui a
été enregistrée une fois dans l'historique Git y reste pour toujours, même après
suppression du fichier.
