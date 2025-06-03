# MemeTrader Pro V3

Bot de trading ultra-agressif basé sur Binance API, avec interface front-end en HTML/CSS/JavaScript et back-end Node.js/Express.  
Permet de simuler (ou exécuter réel/Testnet) des trades automatisés de memecoins toutes les 30 secondes, avec répartition des profits (80 % principal, 20 % rachat).

---

## 📦 Structure du dépôt

```
mon-memetrader/
├── public/
│   └── index.html           ← Interface front-end (HTML + CSS + JS)
├── server.js                ← Serveur Express + Binance API
├── package.json             ← Dépendances (express, binance-api-node@0.12.9, dotenv) + script "start"
├── .gitignore               ← Ignorer node_modules/ et .env
├── README.md                ← Documentation (ce fichier)
└── .env                     ← Variables d’environnement (non versionnées)
```

---

## ⚙️ Prérequis

1. **Node.js** (v16 ou supérieur)  
2. **NPM** (généralement livré avec Node.js)  
3. **Clés Binance API** (Testnet ou Mainnet)  
   - Le fichier `.env` à la racine contient :
     ```
     BINANCE_API_KEY=o9nkH1wRrDVZqz8wxjjmyuqZpCn9HJWPzx5pkXQLgrHfHneisjyrv8Bd1hgfASp2
     BINANCE_API_SECRET=gmLmFpJfVqPcidomNUAkuvLxcmAFrrmY38O3k3RL2EBg5WxnsQlPo7jB1TBQUyJ4
     ```
   - Pour Testnet, remplacez par vos clés Testnet générées sur [Testnet Binance](https://testnet.binance.vision/).

---

## 🚀 Installation & exécution en local

1. **Déplacez-vous** dans le dossier du projet :
   ```bash
   cd mon-memetrader
   ```
2. **Installer les dépendances**  
   ```bash
   npm install
   ```
3. **Lancer le serveur**  
   ```bash
   npm start
   ```
   - Par défaut, le serveur écoute sur [`http://localhost:3000`](http://localhost:3000).

4. **Ouvrir l’interface**  
   - Dans votre navigateur, allez sur :  
     [`http://localhost:3000`](http://localhost:3000)  
   - Cliquez sur **“Déposer des fonds”**, puis **“Activer le Bot”** pour lancer la simulation.

---

## ☁️ Déploiement sur Render

1. **Pousser le code sur GitHub**  
   ```bash
   git init
   git add .
   git commit -m "Projet prêt à déployer"
   git branch -M main
   git remote add origin https://github.com/Borsalino47/mon-memetrader.git
   git push -u origin main
   ```
2. **Créer un Web Service sur Render**  
   - Connectez Render à votre compte GitHub et sélectionnez `mon-memetrader`.  
   - **Root Directory** : laissez vide (ou `.`).  
   - **Build Command** : `npm install`  
   - **Start Command** : `npm start`  
   - **Variables d’environnement** (dans “Environment” / “Variables”) :
     - `BINANCE_API_KEY=o9nkH1wRrDVZqz8wxjjmyuqZpCn9HJWPzx5pkXQLgrHfHneisjyrv8Bd1hgfASp2`  
     - `BINANCE_API_SECRET=gmLmFpJfVqPcidomNUAkuvLxcmAFrrmY38O3k3RL2EBg5WxnsQlPo7jB1TBQUyJ4`  

3. **Déployer**  
   - Render clonera le dépôt, installera (`npm install`) puis démarrera (`npm start`).  
   - L’URL publique (ex. `https://mon-memetrader.onrender.com`) sera disponible sans erreur.

---

## 📝 Licence

Ce projet est sous licence **MIT**.  
