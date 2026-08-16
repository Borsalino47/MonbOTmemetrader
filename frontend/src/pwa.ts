/** Installability and the service worker.
 *
 * Both need a secure context. `http://127.0.0.1:8000` qualifies — the spec
 * treats loopback as secure, which is why this works on the phone without a
 * certificate. Reaching the same server from another device over the LAN
 * (192.168.x.x) does NOT, so there the app still runs but cannot be installed;
 * `installability()` reports which case you are in rather than failing silently.
 */

export type Installability =
  | { state: 'installed' }
  | { state: 'ready' }
  | { state: 'unsupported'; reason: string };

/** True when the app is running from the home screen rather than a browser tab. */
export function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    // iOS Safari predates the media query and uses this instead.
    (window.navigator as { standalone?: boolean }).standalone === true
  );
}

export function installability(): Installability {
  if (isStandalone()) return { state: 'installed' };
  if (!window.isSecureContext) {
    return {
      state: 'unsupported',
      reason:
        "Installation impossible : la page n'est pas dans un contexte sécurisé. " +
        'Ouvrez http://127.0.0.1:8000 sur le téléphone lui-même (et non via son ' +
        "adresse réseau), ou servez l'application en HTTPS.",
    };
  }
  if (!('serviceWorker' in navigator)) {
    return { state: 'unsupported', reason: "Ce navigateur ne gère pas l'installation." };
  }
  return { state: 'ready' };
}

export function registerServiceWorker(): void {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  // After load: registration competes with the first paint otherwise, and on a
  // phone the first paint is the thing being optimised.
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {
      /* A failed registration costs offline shell caching, never the app. */
    });
  });
}
