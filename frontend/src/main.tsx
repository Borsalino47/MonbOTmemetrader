import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { registerServiceWorker } from './pwa';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// Caches the shell so launching from the home screen is instant. It never
// caches /api — a stale price is worse than a slow one.
registerServiceWorker();
