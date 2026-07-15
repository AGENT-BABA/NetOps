/* Firebase Cloud Messaging (FCM) initialization.
 * Gets FCM token after user grants permission, sends it to backend.
 * Enables real push notifications even when the app/browser is closed.
 */
import { initializeApp } from "firebase/app";
import { getMessaging, getToken } from "firebase/messaging";
import { api } from "@/lib/api";
import { NOTIF_ENABLED_KEY } from "@/lib/notify";

const firebaseConfig = {
  apiKey: process.env.REACT_APP_FIREBASE_API_KEY,
  authDomain: process.env.REACT_APP_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.REACT_APP_FIREBASE_PROJECT_ID,
  storageBucket: process.env.REACT_APP_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.REACT_APP_FIREBASE_MESSAGING_SENDER_ID,
  appId: process.env.REACT_APP_FIREBASE_APP_ID,
};

const VAPID_KEY = process.env.REACT_APP_FIREBASE_VAPID_KEY;

let messaging = null;

function isFirebaseConfigured() {
  return !!(firebaseConfig.apiKey && firebaseConfig.projectId && VAPID_KEY);
}

export async function initFCM() {
  try {
    if (!isFirebaseConfigured()) {
      console.warn("FCM: Firebase config missing, skipping FCM init");
      return null;
    }

    if (typeof window === "undefined" || !("serviceWorker" in navigator)) return null;

    if (Notification.permission !== "granted") return null;
    if (localStorage.getItem(NOTIF_ENABLED_KEY) === "off") return null;

    const app = initializeApp(firebaseConfig);
    messaging = getMessaging(app);

    const token = await getToken(messaging, { vapidKey: VAPID_KEY });

    if (token) {
      await saveFCMToken(token);
    }

    return token;
  } catch (err) {
    console.error("FCM init failed:", err);
    return null;
  }
}

export async function saveFCMToken(token) {
  try {
    await api.post("/user/fcm-token", { token });
  } catch (err) {
    console.error("Failed to save FCM token:", err);
  }
}

export function getFCMToken() {
  return getToken(messaging, { vapidKey: VAPID_KEY }).catch(() => null);
}
