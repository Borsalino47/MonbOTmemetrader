import { useEffect, useState } from 'react';
import { installability, isStandalone } from '../pwa';

/** Chrome fires this instead of showing its own prompt, if we listen. */
interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

const DISMISSED_KEY = 'cryptopulse.install-dismissed';

/** Offers to install the app, once, and never again after a refusal.
 *
 * Deliberately quiet: it appears only when the browser has confirmed the app is
 * actually installable, and a dismissal is remembered. A banner that returns on
 * every visit is how a useful prompt becomes something people learn to ignore. */
export function InstallPrompt() {
  const [event, setEvent] = useState<BeforeInstallPromptEvent | null>(null);
  const [hidden, setHidden] = useState(() => {
    try {
      return localStorage.getItem(DISMISSED_KEY) === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    if (isStandalone()) return;
    const onPrompt = (e: Event) => {
      // Suppress Chrome's own mini-infobar so the offer appears where the rest
      // of the interface lives rather than over it.
      e.preventDefault();
      setEvent(e as BeforeInstallPromptEvent);
    };
    const onInstalled = () => setEvent(null);
    window.addEventListener('beforeinstallprompt', onPrompt);
    window.addEventListener('appinstalled', onInstalled);
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt);
      window.removeEventListener('appinstalled', onInstalled);
    };
  }, []);

  if (hidden || !event) return null;

  async function install() {
    if (!event) return;
    await event.prompt();
    await event.userChoice;      // resolved either way; the browser handles the rest
    setEvent(null);
  }

  function dismiss() {
    try { localStorage.setItem(DISMISSED_KEY, '1'); } catch { /* storage off */ }
    setHidden(true);
  }

  return (
    <div className="install-bar">
      <span className="install-text">
        <strong>Installer Crypto Pulse</strong>
        <span>Une icône sur l'écran d'accueil, en plein écran, sans barre du navigateur.</span>
      </span>
      <span className="install-actions">
        <button className="action" onClick={install}>Installer</button>
        <button className="install-dismiss" onClick={dismiss} aria-label="Ne plus proposer">×</button>
      </span>
    </div>
  );
}

/** Says why installation is unavailable, when it is. Shown in the home screen
 *  rather than as a banner: it is information, not a call to action. */
export function InstallStatus() {
  const status = installability();
  if (status.state !== 'unsupported') return null;
  return <p className="home-note">{status.reason}</p>;
}
