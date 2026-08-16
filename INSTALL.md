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

### Étape 1 — Installer Termux

Installez **Termux** depuis **F-Droid** : https://f-droid.org/packages/com.termux/

> N'installez pas Termux depuis le Play Store. La version du Play Store n'est
> plus mise à jour et les paquets Python y sont cassés. C'est la cause d'échec
> la plus fréquente sur Android.

### Étape 2 — Trois commandes

Ouvrez Termux et tapez :

```bash
pkg install -y python git
git clone https://github.com/Borsalino47/MonbOTmemetrader.git
cd MonbOTmemetrader && ./start.sh
```

L'installation prend quelques minutes la première fois (le téléphone compile
`numpy`). Les fois suivantes, le démarrage est immédiat.

### Installer l'application sur l'écran d'accueil

Une seule commande fait tout — dépendances, icônes, interface, démarrage :

```bash
./android-start.sh
```

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

* **Scanner** — le classement en direct.
* **Alerts** — ce qui a franchi un seuil.
* **Verification** — ce que le prix a fait après. **L'onglet qui compte.**
* **Performance** — les statistiques de réussite des signaux déjà tranchés.

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

### « npm: command not found »

Seul le tableau de bord graphique a besoin de `npm`. Le scan en ligne de commande
fonctionne sans. Installez Node.js depuis https://nodejs.org si vous voulez
l'interface.

---

## Recevoir les alertes sur votre téléphone

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
