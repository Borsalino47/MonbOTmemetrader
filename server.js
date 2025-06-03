// server.js
require('dotenv').config();
const path    = require('path');
const express = require('express');
const Binance = require('binance-api-node').default;

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const apiKey    = process.env.BINANCE_API_KEY;
const apiSecret = process.env.BINANCE_API_SECRET;
if (!apiKey || !apiSecret) {
  console.error("ERREUR : Veuillez définir BINANCE_API_KEY et BINANCE_API_SECRET dans le fichier .env");
  process.exit(1);
}

const client = Binance({
  apiKey,
  apiSecret,
});

app.get('/api/price/:symbol', async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const prices = await client.prices({ symbol });
    if (!prices[symbol]) {
      return res.status(400).json({ error: 'Symbole inconnu' });
    }
    return res.json({ symbol, price: parseFloat(prices[symbol]) });
  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: err.message });
  }
});

app.post('/api/order', async (req, res) => {
  try {
    const { symbol, quantity, side } = req.body;
    if (!symbol || !quantity || !side) {
      return res.status(400).json({ error: 'Données incomplètes' });
    }
    const order = await client.order({
      symbol:   symbol.toUpperCase(),
      side:     side.toUpperCase(),
      type:     'MARKET',
      quantity: quantity.toString(),
    });
    return res.json(order);
  } catch (err) {
    console.error(err);
    return res.status(500).json({ error: err.message });
  }
});

app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Serveur démarré sur le port ${PORT}`);
});
